"""A fetcher that serves the sample wiki without touching the network."""

from __future__ import annotations

import asyncio
import logging

from . import sample_wiki
from .html_links import extract_links

log = logging.getLogger(__name__)


class LocalFetcher:
    """Serves the sample wiki.

    Renders each page to HTML and parses it back, rather than handing over the
    graph it already holds. Going the long way round is the point: the parser
    runs on the same path it will run on for real pages.
    """

    site = sample_wiki.SITE

    def __init__(self, site: str | None = None, *, delay_s: float = 0.0) -> None:
        # `site` is accepted so every fetcher constructs alike; this one
        # only ever serves the sample wiki.
        self._delay_s = delay_s
        self.calls = 0

    async def fetch(self, title: str) -> list[str] | None:
        self.calls += 1
        if self._delay_s:
            await asyncio.sleep(self._delay_s)

        html = sample_wiki.render(title)
        if html is None:
            log.debug("        GET %s -> 404", title)
            return None

        # Parsing is pure Python and would otherwise hold the event loop for
        # the whole page. On a free-threaded build this also puts it on another
        # core; on a stock one it only stops the loop stalling.
        links = await asyncio.to_thread(extract_links, html, site=self.site)
        log.debug("        GET %s -> %dB, %d link(s)", title, len(html), len(links))
        return links
