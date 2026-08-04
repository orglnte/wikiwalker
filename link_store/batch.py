"""The links for one batch of titles, some of which may still be arriving."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

from settings import FETCH_TIMEOUT_S

if TYPE_CHECKING:
    from .store import LinkStore

log = logging.getLogger(__name__)

# Returned by `get` for a title the store cannot account for. Distinct from
# None, which means the title is known to have no article.
UNKNOWN: Any = object()


class BatchLinks(Mapping[str, "list[str] | None"]):
    """Links for a batch, resolved on first access.

        [...]    an article and its links
        None     no article behind the title
        absent   could not be read — a gap in the data, or a failed fetch

    A title still being fetched blocks the first access to it, and is written
    to the store as it resolves.
    """

    def __init__(
        self,
        store: LinkStore,
        known: dict[str, list[str] | None],
        pending: dict[str, Future[list[str] | None]],
    ) -> None:
        self._store = store
        self._known = known
        self._pending = pending

    def __getitem__(self, title: str) -> list[str] | None:
        self._resolve(title)
        return self._known[title]

    def __iter__(self) -> Iterator[str]:
        self.drain()
        return iter(self._known)

    def __len__(self) -> int:
        self.drain()
        return len(self._known)

    def drain(self) -> None:
        """Resolve everything still outstanding.

        Pages already fetched are written even if nobody asked for them, so a
        search that ends early does not throw away work it paid for.
        """
        for title in list(self._pending):
            self._resolve(title)

    def _resolve(self, title: str) -> None:
        future = self._pending.pop(title, None)
        if future is None:
            return

        try:
            links = future.result(timeout=FETCH_TIMEOUT_S)
        except TimeoutError:
            log.warning("fetch timed out: %s", title)
            return
        except Exception as exc:
            log.warning("fetch failed: %s (%s: %s)", title, type(exc).__name__, exc)
            return

        self._store.write(title, links)
        self._known[title] = links
