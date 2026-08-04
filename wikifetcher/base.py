"""The fetching interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from settings import MAX_CONCURRENCY, USER_AGENT

__all__ = ["MAX_CONCURRENCY", "USER_AGENT", "Fetcher", "Page", "PageFetchFailed"]


@dataclass(frozen=True)
class Page:
    """What one retrieval found.

    `title` is what the fetch landed on, which is not always what was asked
    for: a title can redirect, and several can lead to the same article.
    `aliases` are the titles passed through on the way, so the caller can
    record them rather than fetch them again.
    """

    title: str
    links: list[str] | None
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PageFetchFailed:
    """A retrieval that produced no page, and why.

    A separate type from `Page` rather than a field on it: with the reason
    inside, a timeout and a 404 would both be a `Page` whose links are None,
    told apart only by a field a reader can forget.
    """

    title: str
    reason: str


class Fetcher(Protocol):
    """Retrieves the outgoing links of one page.

    One page rather than a batch, so the caller can resolve each as it lands.
    Concurrency is the caller's business. Callers never see HTML.
    """

    site: str

    async def fetch(self, title: str) -> Page:
        """The page the title leads to, its links, and any titles redirecting
        to it. `Page.links` is None when no article exists.

        Raises when the page could not be read at all — a different thing from
        an article not existing, and only the fetcher can tell them apart.
        """
        ...
