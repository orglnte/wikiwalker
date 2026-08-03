"""A fetcher that serves the sample wiki without touching the network."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from . import sample_wiki
from .base import MAX_CONCURRENCY
from .html_links import extract_links

log = logging.getLogger(__name__)


class LocalFetcher:
    """Serves the sample wiki.

    Renders each page to HTML and parses it back, rather than handing over the
    graph it already holds. Going the long way round is the point: the parser
    runs on the same path it will run on for real pages.
    """

    site = sample_wiki.SITE

    def __init__(self, *, delay_s: float = 0.0) -> None:
        # A delay makes concurrency visible in the logs; zero by default.
        self._delay_s = delay_s
        self.calls = 0

    async def fetch(self, titles: Iterable[str]) -> dict[str, list[str] | None]:
        wanted = list(titles)
        self.calls += 1
        log.info("  fetch %d page(s), at most %d at once", len(wanted), MAX_CONCURRENCY)

        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(title: str) -> tuple[str, list[str] | None]:
            async with semaphore:
                if self._delay_s:
                    await asyncio.sleep(self._delay_s)
                html = sample_wiki.render(title)
                if html is None:
                    log.debug("    GET %s -> 404", title)
                    return title, None
                links = extract_links(html, site=self.site)
                log.debug("    GET %s -> %dB, %d link(s)", title, len(html), len(links))
                return title, links

        return dict(await asyncio.gather(*(one(title) for title in wanted)))
