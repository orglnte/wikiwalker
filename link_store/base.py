"""What a Walker walks: an object that stores links.

Put a page's links in, get them out, and answer what is known about a title —
including that no article exists behind it, which is different from never having
been asked.

Where the links live is not part of this. SQLite, memory, Redis, a JSON file:
any of them can answer these six questions. What one implementation needs and
another could not provide — an ETag, a connection, a file handle — belongs to
that implementation.

Age is the one storage-ish thing genuinely part of the concept: a store that
cannot say how old a record is cannot be refreshed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class LinkStore(Protocol):
    """What a Walker walks, and what a caching store wraps."""

    def get_links(self, titles: Iterable[str]) -> dict[str, list[str]]:
        """`{title: outgoing links}` for the titles that are articles.

        A title present with an empty list links nowhere. A title absent is
        either unknown or has no article — `red_links` tells those apart.
        """
        ...

    def red_links(self, titles: Iterable[str]) -> set[str]:
        """Which of `titles` are known to have no article behind them."""
        ...

    def status(self, title: str) -> str | None:
        """'ok', 'redlink', or None if the title is unknown."""
        ...

    def store(self, title: str, links: list[str]) -> None:
        """Record a page and its outgoing links, replacing any earlier ones."""
        ...

    def mark_red_links(self, titles: Iterable[str]) -> None:
        """Record that these titles have no article behind them."""
        ...

    def stale_titles(
        self, titles: Iterable[str], max_age_s: float, *, status: str | None = None
    ) -> list[str]:
        """Which known titles were stored longer ago than `max_age_s`."""
        ...
