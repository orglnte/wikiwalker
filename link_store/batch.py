"""The links for one batch of titles, some of which may still be arriving.

Reads. Never writes — what it resolves is handed back for the store to keep.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import logctx
import wikifetcher
from settings import FETCH_TIMEOUT_S, NOT_FOUND_TTL_S

from .db import LinkDatabase, PageType
from .fetcher import LinkFetcher

log = logging.getLogger(__name__)

# The db had no row for this title. Distinct from None, which means the db
# holds the title and knows there is no article behind it.
MISSING: Any = object()

# Returned instead of links when the store has no answer at all: never fetched,
# a failed retrieval, or a budget that ran out.
PAGE_FETCH_FAILED: Any = object()


@dataclass(frozen=True)
class Entry:
    """Where a title's answer came from.

    Both can be set: a title that 404'd more than a day ago is held and
    refetched, and it is the fresh one that should be kept.
    """

    held: list[str] | None = MISSING
    arrived: wikifetcher.Page | wikifetcher.PageFetchFailed | None = None


class BatchLinks:
    """Links for a batch of titles, read one at a time.

    Reading one title waits only for that title, so there is deliberately no
    way to ask for the whole batch at once.
    """

    def __init__(
        self,
        titles: Iterable[str],
        db: LinkDatabase,
        fetcher: LinkFetcher | None = None,
        allowance: int | None = None,
    ) -> None:
        wanted = list(dict.fromkeys(titles))
        self._held = db.get_links(wanted)
        self._pending: dict[str, Future[wikifetcher.Page]] = {}
        self._resolved: dict[str, Entry] = {}
        self._silenced = 0

        if fetcher is None:
            self.submitted = 0
            return

        to_fetch = [title for title in wanted if title not in self._held]

        # A title that 404s becomes an article only when somebody writes one,
        # so these are worth another look but rarely.
        to_fetch += db.stale_titles(
            [t for t, links in self._held.items() if links is None],
            NOT_FOUND_TTL_S,
            page_type=PageType.NOTFOUND,
        )

        # Whatever the allowance will not cover stays unfetched, which reads as
        # a page nobody could read.
        if allowance is not None and len(to_fetch) > allowance:
            log.info(
                "    fetch budget reached: %d page(s) left unread",
                len(to_fetch) - allowance,
            )
            to_fetch = to_fetch[:allowance]

        log.info(
            "    %sbatch of %d: %d held, %d to retrieve",
            logctx.where(), len(wanted), len(self._held), len(to_fetch),
        )
        self.submitted = len(to_fetch)
        if to_fetch:
            self._pending = fetcher.submit(to_fetch)

    def get(self, title: str) -> Entry:
        """Returns the fetched data for the given title, blocks until available"""
        if title not in self._resolved:
            self._resolved[title] = self._wait(title)
        return self._resolved[title]

    def drain(self) -> dict[str, Entry]:
        """Resolve everything still outstanding, and return it.

        Pages already fetched are worth keeping even if nobody asked for them:
        a search that ends early has paid for them either way.
        """
        return {title: self.get(title) for title in list(self._pending)}

    def keep_what_landed(self) -> dict[str, Entry]:
        """Resolve what has already arrived and abandon the rest.

        A batch submits every title at once and a semaphore holds all but a few
        back, so most of what is outstanding has not been sent. Waiting on those
        would start requests nobody is going to read.
        """
        landed = {}
        for title, future in list(self._pending.items()):
            if future.done():
                landed[title] = self.get(title)
            else:
                future.cancel()
                del self._pending[title]
        return landed

    def _wait(self, title: str) -> Entry:
        held = self._held.get(title, MISSING)
        future = self._pending.pop(title, None)
        if future is None:
            return Entry(held=held)

        try:
            return Entry(held=held, arrived=future.result(timeout=FETCH_TIMEOUT_S))
        except TimeoutError:
            reason = f"timed out after {FETCH_TIMEOUT_S:.0f}s"
        except wikifetcher.PageUnavailable as exc:
            reason = f"{type(exc).__name__}: {exc}"
            # A stopped fetcher fails every remaining title for one reason, so
            # it is worth saying once rather than once per page.
            self._silenced += 1
            if self._silenced == 1:
                log.warning("could not read %s (%s)", title, reason)
            elif self._silenced == 2:
                log.warning("the rest of this batch fails the same way")
            return Entry(held=held, arrived=wikifetcher.PageFetchFailed(title, reason))
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"

        log.warning("could not read %s (%s)", title, reason)
        return Entry(held=held, arrived=wikifetcher.PageFetchFailed(title, reason))
