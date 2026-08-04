"""The fetching interface."""

from __future__ import annotations

from typing import Protocol

from settings import MAX_CONCURRENCY, USER_AGENT

__all__ = ["MAX_CONCURRENCY", "USER_AGENT", "Fetcher"]


class Fetcher(Protocol):
    """Retrieves the outgoing links of one page.

    One page rather than a batch, so the caller can resolve each as it lands.
    Concurrency is the caller's business. Callers never see HTML.
    """

    site: str

    async def fetch(self, title: str) -> list[str] | None:
        """The page's outgoing links, or None when no article exists.

        Raises when the page could not be read at all — a different thing from
        an article not existing, and only the fetcher can tell them apart.
        """
        ...
