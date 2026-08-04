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

SCHEMA = """
-- Holds `site`: the same title names different articles on different wikis, so
-- URLs cannot be rendered without it.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A row means the title is known. status: 'ok' = article, its edges are in
-- `links`; 'redlink' = linked to, but no article exists. No row = never
-- fetched. Recording red links is what keeps that third case distinct.
CREATE TABLE IF NOT EXISTS pages (
    title      TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    etag       TEXT,          -- for conditional GETs when refreshing
    status     TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    ord INTEGER NOT NULL,     -- position on the page, so ordering is stable
    PRIMARY KEY (src, ord)
);

CREATE INDEX IF NOT EXISTS ix_links_src ON links(src);
"""

# SQLite's host-parameter cap per statement is 999 on older builds. Every
# IN (...) query batches below it.
_PARAM_BATCH = 900

# A red link only changes when someone writes the article, so re-checking one
# every run buys nothing.
RED_LINK_TTL_S = 24 * 60 * 60


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
        """
        wanted = list(titles)
        found: dict[str, list[str] | None] = {}

        for batch in _batches(wanted, _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))

            # From `pages` first, so an article with no links still gets an
            # entry; `links` alone would omit it.
            for title, status in self._conn.execute(
                f"SELECT title, status FROM pages WHERE title IN ({placeholders})",
                batch,
            ):
                found[title] = [] if status == "ok" else None

            for src, dst in self._conn.execute(
                f"SELECT src, dst FROM links WHERE src IN ({placeholders}) ORDER BY src, ord",
                batch,
            ):
                links = found.get(src)
                if links is not None:
                    links.append(dst)

        return found

    def stale_titles(
        self, titles: Iterable[str], max_age_s: float, *, status: str | None = None
    ) -> list[str]:
        """Return the known titles whose record is older than `max_age_s`.

        `status` narrows the check to one kind: articles and red links get
        different lifetimes (see `RED_LINK_TTL_S`).
        """
        cutoff = time.time() - max_age_s
        stale: list[str] = []

        clause = " AND status = ?" if status else ""
        extra = [status] if status else []

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

    def red_links(self, titles: Iterable[str]) -> set[str]:
        """Return which of `titles` are known to have no article behind them."""
        known: set[str] = set()

        for batch in _batches(list(titles), _PARAM_BATCH):
            placeholders = ",".join("?" * len(batch))
            known.update(
                title
                for (title,) in self._conn.execute(
                    f"SELECT title FROM pages "
                    f"WHERE status = 'redlink' AND title IN ({placeholders})",
                    batch,
                )
            )

        return known

    def status(self, title: str) -> str | None:
        """'ok', 'redlink', or None if the title appears nowhere at all.

        Single-title counterpart to the batch methods, for a search's endpoints.
        """
        row = self._conn.execute(
            "SELECT status FROM pages WHERE title = ?", (title,)
        ).fetchone()
        return row[0] if row else None

    def red_link_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status = 'redlink'"
        ).fetchone()[0]

    def store(self, title: str, links: list[str], etag: str | None = None) -> None:
        """Record a fetched page and its links in one transaction.

        Split across two, an interrupted write leaves a page marked fetched with
        no edges — indistinguishable from a real dead end.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO pages (title, fetched_at, etag, status) "
                "VALUES (?, ?, ?, 'ok')",
                (title, time.time(), etag),
            )
            # Replace, not append, so a refresh reflects removed links too.
            self._conn.execute("DELETE FROM links WHERE src = ?", (title,))
            self._conn.executemany(
                "INSERT INTO links (src, dst, ord) VALUES (?, ?, ?)",
                [(title, dst, i) for i, dst in enumerate(links)],
            )

    def mark_red_links(self, titles: Iterable[str]) -> None:
        """Record that these titles have no article behind them (e.g. a 404).

        Replaces any existing row and drops its edges: an article can be
        deleted, and the search must not keep walking out of a dead page.
        """
        now = time.time()
        with self._conn:
            for title in titles:
                self._conn.execute(
                    "INSERT OR REPLACE INTO pages (title, fetched_at, etag, status) "
                    "VALUES (?, ?, NULL, 'redlink')",
                    (title, now),
                )
                self._conn.execute("DELETE FROM links WHERE src = ?", (title,))

    def touch(self, title: str) -> None:
        """Move only the timestamp — for a conditional GET returning 304."""
        with self._conn:
            self._conn.execute(
                "UPDATE pages SET fetched_at = ? WHERE title = ?", (time.time(), title)
            )

    def page_count(self) -> int:
        """Number of real articles. Red links are known titles, not articles."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM pages WHERE status = 'ok'"
        ).fetchone()[0]

    def link_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
