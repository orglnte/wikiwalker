"""SQLite storage for page links: a cache of "what does page X link to".

Holds nothing about any particular search, so it is reusable and can be filled
from a scrape or a dump.

Two tables, not one: `pages` records what is known about a title, `links` holds
the edges. A links table alone cannot distinguish "fetched, links nowhere" from
"never fetched" — both are zero rows.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator
from enum import StrEnum

from settings import SQLITE_PARAM_BATCH as _PARAM_BATCH


class PageType(StrEnum):
    """The only values a `pages` row can carry."""

    ARTICLE = "article"
    REDIRECT = "redirect"
    NOTFOUND = "notfound"


_TYPE_VALUES = ", ".join(f"'{type}'" for type in PageType)

SCHEMA = f"""
-- Holds `site`: the same title names different articles on different wikis, so
-- URLs cannot be rendered without it.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A row means the title is known:
--   'article'   the title served a page, and its links are in `links`
--   'redirect'  the title redirects to another, named in `redirect_to`
--   'notfound'  the title returned 404
-- Never fetched has no row at all, which is what recording a 404 keeps distinct
-- from a dead end. A link to a 'notfound' title is a red link; that is a fact
-- about the link, so it lives in `links` rather than here.
CREATE TABLE IF NOT EXISTS pages (
    title       TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    etag        TEXT,          -- for conditional GETs when refreshing
    type      TEXT NOT NULL CHECK (type IN ({_TYPE_VALUES})),

    -- Set only on 'redirect', and followed exactly one hop, as a wiki does.
    -- A redirect naming a redirect is served as that page, not followed on.
    redirect_to TEXT
);

CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    ord INTEGER NOT NULL,     -- links are stored sorted, so ordering is stable
    PRIMARY KEY (src, ord)
);

CREATE INDEX IF NOT EXISTS ix_links_src ON links(src);
"""



class LinkDatabase:
    """Stores and retrieves the outgoing links of pages."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)

        # Reads run concurrently with a writer; the search interleaves both.
        self._conn.execute("PRAGMA journal_mode=WAL")

        # Cache data — a crash losing the last few pages just re-fetches them.
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LinkDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    def get_links(self, titles: Iterable[str]) -> dict[str, list[str] | None]:
        """What is known about each title, in one answer.

            [...]    an article, and these are its links (empty = links nowhere)
            None     no article behind this title
            absent   never fetched

        A redirect answers with what it points at. Following it is not a step:
        a link to a redirect lands on the article in one click, so counting it
        as a hop would make every path through one come out too long.

        Exactly one hop, as a wiki does. A redirect naming another redirect is
        served as that second page rather than followed on, so a reader gets
        the same one link out of it that we do.
        """
        wanted = list(titles)
        answers: dict[str, str] = {}            # title -> the title holding its links
        found: dict[str, list[str] | None] = {}

        for batch in _batches(wanted, _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            for title, type, destination in self._conn.execute(
                f"SELECT title, type, redirect_to FROM pages WHERE title IN ({placeholders})",
                batch,
            ):
                if type == PageType.REDIRECT and destination:
                    answers[title] = destination
                    continue

                answers[title] = title
                # From `pages` first, so an article with no links still gets an
                # entry; `links` alone would omit it.
                found[title] = [] if type == PageType.ARTICLE else None

        # What a redirect names may not have been asked for, and may not be held
        # at all — in which case the redirect reads as absent, like it. Landing
        # on a second redirect spends the hop: its page is the one link it holds.
        for batch in _batches(sorted(set(answers.values()) - set(found)), _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            for title, type, destination in self._conn.execute(
                f"SELECT title, type, redirect_to FROM pages WHERE title IN ({placeholders})",
                batch,
            ):
                if type == PageType.REDIRECT:
                    found[title] = [destination] if destination else []
                else:
                    found[title] = [] if type == PageType.ARTICLE else None

        for batch in _batches(sorted(found), _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            for src, dst in self._conn.execute(
                f"SELECT src, dst FROM links WHERE src IN ({placeholders}) ORDER BY src, ord",
                batch,
            ):
                links = found.get(src)
                if links is not None:
                    links.append(dst)

        return {
            title: found[held]
            for title, held in answers.items()
            if held in found
        }

    def stale_titles(
        self, titles: Iterable[str], max_age_s: float, *, type: PageType | None = None
    ) -> list[str]:
        """Return the known titles whose record is older than `max_age_s`.

        `type` narrows the check to one kind: an article and a title that
        404s get different lifetimes (see `NOT_FOUND_TTL_S`).
        """
        cutoff = time.time() - max_age_s
        stale: list[str] = []

        clause = " AND type = ?" if type else ""
        extra = [type] if type else []

        for batch in _batches(list(titles), _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            stale.extend(
                title
                for (title,) in self._conn.execute(
                    f"SELECT title FROM pages "
                    f"WHERE title IN ({placeholders}) AND fetched_at < ?{clause}",
                    [*batch, cutoff, *extra],
                )
            )

        return stale

    def get_etag(self, title: str) -> str | None:
        row = self._conn.execute(
            "SELECT etag FROM pages WHERE title = ?", (title,)
        ).fetchone()
        return row[0] if row else None

    def not_found(self, titles: Iterable[str]) -> set[str]:
        """Return which of `titles` are known to have no article behind them."""
        known: set[str] = set()

        for batch in _batches(list(titles), _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            known.update(
                title
                for (title,) in self._conn.execute(
                    f"SELECT title FROM pages "
                    f"WHERE type = ? AND title IN ({placeholders})",
                    [PageType.NOTFOUND, *batch],
                )
            )

        return known

    def page_type(self, title: str) -> PageType | None:
        """The title's type, or None if it appears nowhere at all.

        Single-title counterpart to the batch methods, for a search's endpoints.
        """
        row = self._conn.execute(
            "SELECT type FROM pages WHERE title = ?", (title,)
        ).fetchone()
        return PageType(row[0]) if row else None

    def not_found_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM pages WHERE type = ?", (PageType.NOTFOUND,)
        ).fetchone()[0]

    def store(self, title: str, links: list[str], etag: str | None = None) -> None:
        """Record a fetched page and its links in one transaction.

        Split across two, an interrupted write leaves a page marked fetched with
        no edges — indistinguishable from a real dead end.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pages (title, fetched_at, etag, type) "
                "VALUES (?, ?, ?, ?)",
                (title, time.time(), etag, PageType.ARTICLE),
            )
            # Replace, not append, so a refresh reflects removed links too.
            self._conn.execute("DELETE FROM links WHERE src = ?", (title,))
            self._conn.executemany(
                "INSERT INTO links (src, dst, ord) VALUES (?, ?, ?)",
                [(title, dst, i) for i, dst in enumerate(sorted(links))],
            )

    def bulk_write(self, pages: Iterable[tuple[str, list[str] | None]]) -> tuple[int, int]:
        """Record many pages at once. `None` links mean no article exists.

        One transaction for the lot, so an interrupted load leaves the store as
        it was. Returns (articles, missing titles) written.
        """
        now = time.time()
        articles: list[tuple[str, float]] = []
        dead: list[tuple[str, float]] = []
        edges: list[tuple[str, str, int]] = []
        replaced: list[tuple[str]] = []

        for title, links in pages:
            replaced.append((title,))
            if links is None:
                dead.append((title, now))
            else:
                articles.append((title, now))
                edges.extend((title, dst, i) for i, dst in enumerate(sorted(links)))

        with self._conn:
            self._conn.executemany("DELETE FROM links WHERE src = ?", replaced)
            self._conn.executemany(
                "INSERT OR REPLACE INTO pages (title, fetched_at, etag, type) "
                "VALUES (?, ?, NULL, ?)",
                [(title, at, PageType.ARTICLE) for title, at in articles],
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO pages (title, fetched_at, etag, type) "
                "VALUES (?, ?, NULL, ?)",
                [(title, at, PageType.NOTFOUND) for title, at in dead],
            )
            self._conn.executemany(
                "INSERT INTO links (src, dst, ord) VALUES (?, ?, ?)", edges
            )

        return len(articles), len(dead)

    def build_from_staging(self, namespace: str) -> None:
        """Fill `pages` and `links` from staged MediaWiki tables.

        The joins belong to whoever staged the dump; the column names belong
        here. Kept in one place so the schema stays private to this module.

        Expects `t_page`, `t_pagelinks`, `t_redirect` and `t_target` to exist.
        """
        with self._conn:
            self._conn.executescript(
                f"""
                DELETE FROM links;
                DELETE FROM pages;

                -- Edges as the pages write them. A link to a redirect stays a
                -- link to that title; what it names is the redirect's own row.
                INSERT INTO links (src, dst, ord)
                SELECT p.page_title,
                       t.title,
                       ROW_NUMBER() OVER (PARTITION BY p.page_title ORDER BY t.title) - 1
                FROM t_pagelinks e
                JOIN t_page   p ON p.page_id = e.pl_from
                               AND p.page_namespace = '{namespace}'
                               AND p.page_is_redirect = '0'
                JOIN t_target t ON t.target_id = e.pl_target_id
                WHERE e.pl_from_namespace = '{namespace}';

                -- Every article, including ones linking nowhere: a snapshot is
                -- complete by construction, so no links is knowledge.
                INSERT INTO pages (title, fetched_at, etag, type)
                SELECT page_title, strftime('%s', 'now'), NULL, '{PageType.ARTICLE}'
                FROM t_page
                WHERE page_namespace = '{namespace}' AND page_is_redirect = '0';

                INSERT OR REPLACE INTO pages (title, fetched_at, etag, type, redirect_to)
                SELECT p.page_title, strftime('%s', 'now'), NULL,
                       '{PageType.REDIRECT}', r.rd_title
                FROM t_page p
                JOIN t_redirect r ON r.rd_from = p.page_id
                                 AND r.rd_namespace = '{namespace}'
                WHERE p.page_namespace = '{namespace}' AND p.page_is_redirect = '1';

                -- Titles linked to that have no page of their own. They
                -- outnumber articles several times over; without them the
                -- search cannot tell "no such article" from "not looked at yet".
                INSERT OR IGNORE INTO pages (title, fetched_at, etag, type)
                SELECT DISTINCT t.title, strftime('%s', 'now'), NULL, '{PageType.NOTFOUND}'
                FROM t_target t
                WHERE t.title NOT IN (SELECT title FROM pages);
                """
            )

    def mark_redirects(self, aliases: Iterable[str], destination: str) -> None:
        """Record that these titles all name `destination`.

        Their own edges go: a redirect has none of its own, and a title that
        used to be an article can become one.
        """
        now = time.time()
        with self._conn:
            for alias in aliases:
                self._conn.execute(
                    "INSERT OR REPLACE INTO pages "
                    "(title, fetched_at, etag, type, redirect_to) VALUES (?, ?, NULL, ?, ?)",
                    (alias, now, PageType.REDIRECT, destination),
                )
                self._conn.execute("DELETE FROM links WHERE src = ?", (alias,))

    def destination(self, title: str) -> str | None:
        """What this title redirects to, or None if it is not a redirect."""
        row = self._conn.execute(
            "SELECT redirect_to FROM pages WHERE title = ? AND type = ?",
            (title, PageType.REDIRECT),
        ).fetchone()
        return row[0] if row else None

    def mark_not_found(self, titles: Iterable[str]) -> None:
        """Record that these titles have no article behind them (e.g. a 404).

        Replaces any existing row and drops its edges: an article can be
        deleted, and the search must not keep walking out of a dead page.
        """
        now = time.time()
        with self._conn:
            for title in titles:
                self._conn.execute(
                    "INSERT OR REPLACE INTO pages (title, fetched_at, etag, type) "
                    "VALUES (?, ?, NULL, ?)",
                    (title, now, PageType.NOTFOUND),
                )
                self._conn.execute("DELETE FROM links WHERE src = ?", (title,))

    def forget(self, title: str) -> tuple[int, int]:
        """Remove a title and its links, returning it to `unknown`.

        Not the same as marking it not found: that records "no article
        exists", this records nothing at all, so the next walk fetches it.
        """
        with self._conn:
            links = self._conn.execute(
                "DELETE FROM links WHERE src = ?", (title,)
            ).rowcount
            pages = self._conn.execute(
                "DELETE FROM pages WHERE title = ?", (title,)
            ).rowcount
        return pages, links

    def touch(self, title: str) -> None:
        """Move only the timestamp — for a conditional GET returning 304."""
        with self._conn:
            self._conn.execute(
                "UPDATE pages SET fetched_at = ? WHERE title = ?", (time.time(), title)
            )

    def page_count(self) -> int:
        """Number of real articles. Red links are known titles, not articles."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM pages WHERE type = ?", (PageType.ARTICLE,)
        ).fetchone()[0]

    def link_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
