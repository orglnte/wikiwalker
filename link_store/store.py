"""The store of links a Walker walks."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import wikifetcher

from .batch import BatchLinks
from .db import RED_LINK_TTL_S, LinkDatabase
from .fetcher import LinkFetcher

log = logging.getLogger(__name__)

# One store per dataset. A name not listed here is taken as a path, so tests
# can ask for ":memory:".
DB_FILE = {
    "test": "test.db",
    "simplewiki": "simplewiki.db",
    "wikipedia-us": "wikipedia-us.db",
}


class LinkStore:
    """Holds the links between pages, and retrieves what it does not hold.

    Given a batch of titles it answers from the database, and hands whatever is
    missing to the fetcher. The answer is a mapping either way, so nothing
    upstream learns which pages came off disk and which came off the wire.

    Without a fetcher it answers only from what it holds, which is the case
    when walking a loaded dump.
    """

    def __init__(self, name: str, fetcher: type[wikifetcher.Fetcher] | None = None) -> None:
        self._db = LinkDatabase(DB_FILE.get(name, name))
        self._fetcher = LinkFetcher(fetcher()) if fetcher is not None else None
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

            if to_fetch:
                log.info(
                    "batch of %d: %d held, %d to retrieve",
                    len(wanted), len(known), len(to_fetch),
                )
                pending = self._fetcher.submit(to_fetch)
            else:
                log.info("batch of %d: all held", len(wanted))

        self._open = BatchLinks(self, known, pending)
        return self._open

    def status(self, title: str) -> str | None:
        """'ok', 'redlink', or None if the title is unknown — retrieving first."""
        known = self._db.status(title)
        if known is None and self._fetcher is not None:
            self.get_links([title]).get(title)
            known = self._db.status(title)
        return known

    def write(self, title: str, links: list[str] | None) -> None:
        """Record what a retrieval found. Called by the batch as pages land."""
        if links is None:
            self.mark_red_links([title])
            log.info("  no article at: %s", title)
        else:
            self.store(title, links)
            log.debug("  store %s (%d link(s))", title, len(links))

    def store(self, title: str, links: list[str]) -> None:
        self._db.store(title, links)

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
