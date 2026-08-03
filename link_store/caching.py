"""A link store that fills itself from a fetcher when it comes up short."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import wikifetcher

from .base import LinkStore
from .db import RED_LINK_TTL_S

log = logging.getLogger(__name__)


class CachingLinkStore:
    """A link store that fills itself from a fetcher when it comes up short.

    Wraps any other store — SQLite, memory, a file — and is itself one, so it
    can be given wherever a store is expected. What differs from the store it
    wraps is the meaning of "unknown": there it is a gap in the data, here it is
    a page nobody has asked for yet, and asking fills it.
    """

    def __init__(self, store: LinkStore, fetcher: wikifetcher.Fetcher) -> None:
        self._store = store
        self._fetcher = fetcher
        self.fetched = 0

    def get_links(self, titles: Iterable[str]) -> dict[str, list[str]]:
        wanted = list(dict.fromkeys(titles))

        found = self._store.get_links(wanted)
        dead = self._store.red_links(wanted)
        unknown = [t for t in wanted if t not in found and t not in dead]

        # A red link becomes an article only when somebody writes one, so these
        # are worth another look but rarely.
        stale = self._store.stale_titles(dead, RED_LINK_TTL_S, status="redlink")

        if unknown or stale:
            log.info(
                "asked for %d: %d stored, %d dead, %d to fetch",
                len(wanted), len(found), len(dead), len(unknown) + len(stale),
            )
            self._fill(unknown + stale)
            found = self._store.get_links(wanted)
        else:
            log.info("asked for %d: all stored", len(wanted))

        return found

    def red_links(self, titles: Iterable[str]) -> set[str]:
        # Called only after `get_links` has already tried to fetch these.
        return self._store.red_links(titles)

    def status(self, title: str) -> str | None:
        known = self._store.status(title)
        if known is None:
            self._fill([title])
            known = self._store.status(title)
        return known

    def store(self, title: str, links: list[str]) -> None:
        self._store.store(title, links)

    def mark_red_links(self, titles: Iterable[str]) -> None:
        self._store.mark_red_links(titles)

    def stale_titles(
        self, titles: Iterable[str], max_age_s: float, *, status: str | None = None
    ) -> list[str]:
        return self._store.stale_titles(titles, max_age_s, status=status)

    def _fill(self, titles: list[str]) -> None:
        pages = asyncio.run(self._fetcher.fetch(titles))
        self.fetched += len(pages)

        red: list[str] = []
        for title, links in pages.items():
            if links is None:
                red.append(title)
                continue
            self._store.store(title, links)
            log.debug("  store %s (%d link(s))", title, len(links))

        if red:
            self._store.mark_red_links(red)
            log.info("  no article at: %s", ", ".join(sorted(red)))

        # Titles the fetcher dropped are neither stored nor marked, so the next
        # search treats them as unknown and tries again.
        failed = set(titles) - set(pages)
        if failed:
            log.warning("%d fetch(es) failed: %s", len(failed), ", ".join(sorted(failed)))
