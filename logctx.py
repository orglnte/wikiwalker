"""Where in a walk a log line came from.

The store and the fetcher log while a walk is in progress, but neither knows
what depth it is at — that is the search's concept, and passing it down would
put it into interfaces that have no use for it. A context variable carries it
for logging only.
"""

from __future__ import annotations

from contextvars import ContextVar

depth: ContextVar[int | None] = ContextVar("depth", default=None)


def where() -> str:
    """`"depth N "` while walking, empty otherwise."""
    current = depth.get()
    return f"depth {current} " if current is not None else ""
