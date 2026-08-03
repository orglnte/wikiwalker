"""The fetching interface."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

# Ten in flight: the cap the brief names, and polite regardless.
MAX_CONCURRENCY = 10


class Fetcher(Protocol):
    """Retrieves the outgoing links of a page, by title.

    Takes many titles rather than one: that is what makes the requests
    concurrent, and it matches how the search asks for work — a whole BFS level
    at a time.

    How a page becomes a list of links is the fetcher's business. Callers never
    see HTML.
    """

    site: str

    async def fetch(self, titles: Iterable[str]) -> dict[str, list[str] | None]:
        """Return `{title: outgoing links}`, with None where no article exists.

        Titles omitted from the result could not be fetched at all.
        """
        ...
