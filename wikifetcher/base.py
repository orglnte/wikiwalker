"""The fetching interface."""

from __future__ import annotations

from typing import Protocol

# Ten in flight: the cap the brief names, and polite regardless. Enforced by
# whoever drives the fetcher, not by the fetcher itself.
MAX_CONCURRENCY = 10


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
