"""Retrieving links that the store does not hold yet.

Runs one event loop on a background thread, so callers stay synchronous and get
a `Future` per title. Each resolves the moment its page lands, rather than when
the whole batch does.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable
from concurrent.futures import Future

import wikifetcher

log = logging.getLogger(__name__)


class LinkFetcher:
    """Fetches pages concurrently and hands back their links.

    Holds the concurrency cap: the fetcher underneath deals with one page at a
    time and knows nothing about how many are in flight.
    """

    def __init__(
        self, fetcher: wikifetcher.Fetcher, *, concurrency: int = wikifetcher.MAX_CONCURRENCY
    ) -> None:
        self._fetcher = fetcher
        self._concurrency = concurrency
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="link-fetcher")
        self._thread.start()
        self._limit = asyncio.run_coroutine_threadsafe(self._make_limit(), self._loop).result()
        self.fetched = 0

    @property
    def site(self) -> str:
        return self._fetcher.site

    def submit(self, titles: Iterable[str]) -> dict[str, Future[list[str] | None]]:
        """Start fetching, and return a future per title.

        A future resolves to the page's links, to None when no article exists,
        or raises when the page could not be read.
        """
        wanted = list(titles)
        log.info("  fetch %d page(s), at most %d at once", len(wanted), self._concurrency)
        return {
            title: asyncio.run_coroutine_threadsafe(self._fetch(title), self._loop)
            for title in wanted
        }

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def _make_limit(self) -> asyncio.Semaphore:
        # Built on the loop's own thread; a Semaphore binds to the running loop.
        return asyncio.Semaphore(self._concurrency)

    async def _fetch(self, title: str) -> list[str] | None:
        async with self._limit:
            links = await self._fetcher.fetch(title)
        self.fetched += 1
        return links

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
