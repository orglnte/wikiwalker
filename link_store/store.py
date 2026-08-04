"""The store of links a Walker walks."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

import logctx
import wikifetcher
from settings import DB_FILE, RED_LINK_TTL_S, SITE

from .batch import BatchLinks
from .db import LinkDatabase
from .fetcher import LinkFetcher

log = logging.getLogger(__name__)

# Stored status -> what it means. Absent from the store is a state too.
STATE = {"ok": "article", "redlink": "redlink", None: "unknown"}


class LinkStore:
    """Holds the links between pages, and retrieves what it does not hold.

    Given a batch of titles it answers from the database, and hands whatever is
    missing to the fetcher. The answer is a mapping either way, so nothing
    upstream learns which pages came off disk and which came off the wire.

    Without a fetcher it answers only from what it holds, which is the case
    when walking a loaded dump.
    """

    def __init__(
        self,
        name: str,
        fetcher: Callable[[str | None], wikifetcher.Fetcher] | None = None,
        *,
        concurrency: int = wikifetcher.MAX_CONCURRENCY,
        max_fetched: int | None = None,
    ) -> None:
        self._max_fetched = max_fetched
        self.fetched = 0
        self._db = LinkDatabase(DB_FILE.get(name, name))
        self._fetcher = (
            LinkFetcher(fetcher(SITE.get(name)), concurrency=concurrency)
            if fetcher is not None
            else None
        )
        self._open: BatchLinks | None = None

        # A fetcher decides which wiki this store holds; a loaded one already
        # knows.
        if self._fetcher is not None:
            self._db.set_meta("site", self._fetcher.site)

    def __enter__(self) -> LinkStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def site(self) -> str | None:
        return self._db.get_meta("site")

    def page_count(self) -> int:
        return self._db.page_count()

    def get_links(self, titles: Iterable[str]) -> BatchLinks:
        """Links for these titles. Ones not held yet are retrieved."""
        # Anything still outstanding from the previous batch is worth keeping.
        if self._open is not None:
            self._open.drain()

        wanted = list(dict.fromkeys(titles))
        known = self._db.get_links(wanted)

        pending = {}
        if self._fetcher is not None:
            to_fetch = [t for t in wanted if t not in known]

            # A red link becomes an article only when somebody writes one, so
            # these are worth another look but rarely.
            stale = self._db.stale_titles(
                [t for t, links in known.items() if links is None],
                RED_LINK_TTL_S,
                status="redlink",
            )
            to_fetch.extend(stale)

            # Whatever the budget will not cover stays absent, which the search
            # already reads as "could not be read".
            if self._max_fetched is not None:
                allowed = max(0, self._max_fetched - self.fetched)
                if len(to_fetch) > allowed:
                    log.info(
                        "    fetch budget %d reached: %d page(s) left unread",
                        self._max_fetched, len(to_fetch) - allowed,
                    )
                    to_fetch = to_fetch[:allowed]

            log.info(
                "    %sbatch of %d: %d held, %d to retrieve",
                logctx.where(), len(wanted), len(known), len(to_fetch),
            )
            if to_fetch:
                self.fetched += len(to_fetch)
                pending = self._fetcher.submit(to_fetch)

        self._open = BatchLinks(self, known, pending)
        return self._open

    def status(self, title: str) -> str | None:
        """'ok', 'redlink', or None if the title is unknown — retrieving first."""
        known = self._db.status(title)
        if known is None and self._fetcher is not None:
            self.get_links([title]).get(title)
            known = self._db.status(title)
        return known

    def state(self, title: str) -> str:
        """What is known about a title: article, redlink, or unknown.

        All three are properties of the page itself, so none of them change
        when some other page does.
        """
        return STATE[self.status(title)]

    def write(self, title: str, links: list[str] | None) -> None:
        """Record what a retrieval found. Called by the batch as pages land."""
        if links is None:
            self.mark_red_links([title])
            log.info("      no article at: %s", title)
        else:
            self.store(title, links)
            log.debug("      store %s (%d link(s))", title, len(links))

    def store(self, title: str, links: list[str]) -> None:
        self._db.store(title, links)

    def bulk_write(self, pages: Iterable[tuple[str, list[str] | None]]) -> tuple[int, int]:
        """Record many pages at once. `None` links mean no article exists."""
        return self._db.bulk_write(pages)

    def forget(self, title: str) -> tuple[int, int]:
        """Drop a title from the store so the next walk retrieves it again."""
        return self._db.forget(title)

    def mark_red_links(self, titles: Iterable[str]) -> None:
        self._db.mark_red_links(titles)

    def link_count(self) -> int:
        return self._db.link_count()

    def red_link_count(self) -> int:
        return self._db.red_link_count()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self._db.get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self._db.set_meta(key, value)

    def close(self) -> None:
        if self._open is not None:
            self._open.drain()
        if self._fetcher is not None:
            self._fetcher.close()
        self._db.close()
