"""A fetcher that reads pages over HTTP."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from settings import (
    HTTP_BACKOFF_S,
    HTTP_RETRIES,
    HTTP_TIMEOUT_S,
    MIN_REQUEST_INTERVAL_S,
    RETRYABLE_STATUS,
    USER_AGENT,
)

from .html_links import extract_links
from .titles import DEFAULT_SITE, to_url

log = logging.getLogger(__name__)


class PageUnavailable(Exception):
    """The page could not be read. Distinct from it not existing."""


class RateLimited(PageUnavailable):
    """The site asked us to stop. Nothing further is sent."""


class HttpFetcher:
    """Reads one wiki page per call.

    Only a 404 counts as "no article". Everything else that goes wrong raises,
    so a timeout is never mistaken for a title nobody has written — that
    mistake would store a live article as a dead end and the search would
    quietly return longer paths.
    """

    def __init__(
        self,
        site: str | None = None,
        *,
        timeout_s: float = HTTP_TIMEOUT_S,
        retries: int = HTTP_RETRIES,
        backoff_s: float = HTTP_BACKOFF_S,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
    ) -> None:
        self.site = site or DEFAULT_SITE
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_s = backoff_s
        self._min_interval_s = min_interval_s
        self._client: httpx.AsyncClient | None = None
        self._stopped = False
        self._pace_lock = asyncio.Lock()
        self._last_sent = 0.0

    async def fetch(self, title: str) -> list[str] | None:
        # One 429 stops the whole fetcher. Ten requests each backing off on
        # their own is not backing off.
        if self._stopped:
            raise RateLimited(f"{title}: not sent, already rate limited")

        client = self._ensure_client()
        url = to_url(title, self.site)

        for attempt in range(self._retries + 1):
            await self._pace()
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise PageUnavailable(f"{title}: {exc}") from exc
                await self._back_off(attempt, title, str(exc))
                continue

            if response.status_code == 404:
                log.debug("    GET %s -> 404", title)
                return None

            if response.status_code == 429:
                if not self._stopped:
                    self._stopped = True
                    log.error(
                        "%s is rate limiting us (Retry-After: %s) — stopping. "
                        "Nothing further will be requested.",
                        self.site, response.headers.get("retry-after", "unset"),
                    )
                raise RateLimited(f"{title}: HTTP 429")

            if response.status_code in RETRYABLE_STATUS:
                if attempt == self._retries:
                    raise PageUnavailable(f"{title}: HTTP {response.status_code}")
                await self._back_off(attempt, title, f"HTTP {response.status_code}")
                continue

            if response.status_code != 200:
                raise PageUnavailable(f"{title}: HTTP {response.status_code}")

            html = response.text
            links = await asyncio.to_thread(extract_links, html, site=self.site)
            log.debug("    GET %s -> %dB, %d link(s)", title, len(html), len(links))
            return links

        raise PageUnavailable(title)  # unreachable; the loop always returns or raises

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        # Built on first use so it binds to the loop that will drive it.
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout_s,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def _pace(self) -> None:
        """Hold requests to `min_interval_s` apart, however many are in flight."""
        if not self._min_interval_s:
            return
        async with self._pace_lock:
            wait = self._min_interval_s - (time.monotonic() - self._last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent = time.monotonic()

    async def _back_off(self, attempt: int, title: str, reason: str) -> None:
        delay = self._backoff_s * 2**attempt
        log.warning("retrying %s in %.1fs (%s)", title, delay, reason)
        await asyncio.sleep(delay)
