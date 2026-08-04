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
    MAX_CONSECUTIVE_FAILURES,
    MAX_RATE_LIMIT_PAUSES,
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


class SiteUnreachable(PageUnavailable):
    """Too many pages failed in a row for the site to be up."""


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
        failure_limit: int | None = MAX_CONSECUTIVE_FAILURES,
        pause_limit: int = MAX_RATE_LIMIT_PAUSES,
    ) -> None:
        self._failure_limit = failure_limit
        self._failures = 0
        self._pause_limit = pause_limit
        self._pauses = 0
        self._resume_at = 0.0
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
            raise RateLimited(f"{title}: not sent, the fetcher has stopped")

        try:
            links = await self._read(title)
        except PageUnavailable:
            self._note_failure()
            raise

        self._failures = 0
        return links

    def _note_failure(self) -> None:
        """One page failing is that page's problem; a run of them is the site's."""
        self._failures += 1
        if self._failure_limit is None or self._failures < self._failure_limit:
            return
        self._stopped = True
        log.error(
            "%d pages failed in a row — stopping. Pass --walk-anyway to keep "
            "going on whatever the store already holds.",
            self._failures,
        )
        raise SiteUnreachable(f"{self._failures} consecutive failures")

    async def _read(self, title: str) -> list[str] | None:
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
                log.debug("        GET %s -> 404", title)
                return None

            if response.status_code == 429:
                self._hold_off(response.headers.get("retry-after"))
                continue

            if response.status_code in RETRYABLE_STATUS:
                if attempt == self._retries:
                    raise PageUnavailable(f"{title}: HTTP {response.status_code}")
                await self._back_off(attempt, title, f"HTTP {response.status_code}")
                continue

            if response.status_code != 200:
                raise PageUnavailable(f"{title}: HTTP {response.status_code}")

            html = response.text
            links = await asyncio.to_thread(extract_links, html, site=self.site)
            log.debug("        GET %s -> %dB, %d link(s)", title, len(html), len(links))
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

    def _hold_off(self, retry_after: str | None) -> None:
        """Make every request wait, not just this one.

        Retry-After is a resume time, so it is honoured — but by one shared
        gate. Ten requests each backing off on their own is ten more knocks.
        """
        self._pauses += 1
        if self._pauses > self._pause_limit:
            self._stopped = True
            log.error(
                "%s rate limited us %d times — stopping. Pass --walk-anyway to "
                "keep going on whatever the store already holds.",
                self.site, self._pauses - 1,
            )
            raise RateLimited(f"rate limited {self._pauses - 1} times")

        delay = (
            float(retry_after)
            if retry_after and retry_after.isdigit()
            else self._backoff_s * 2**self._pauses
        )
        self._resume_at = time.monotonic() + delay
        log.warning(
            "%s asked us to wait %.0fs (Retry-After: %s) — pausing everything",
            self.site, delay, retry_after or "unset",
        )

    async def _pace(self) -> None:
        """Hold requests apart, and behind any pause the site asked for."""
        async with self._pace_lock:
            now = time.monotonic()
            wait = max(
                self._resume_at - now,
                self._min_interval_s - (now - self._last_sent),
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent = time.monotonic()

    async def _back_off(self, attempt: int, title: str, reason: str) -> None:
        delay = self._backoff_s * 2**attempt
        log.warning("retrying %s in %.1fs (%s)", title, delay, reason)
        await asyncio.sleep(delay)
