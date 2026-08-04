"""The store of links a Walker walks."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

import logctx
import wikifetcher
from settings import DB_FILE, FETCH_TIMEOUT_S, RED_LINK_TTL_S, SITE

from .batch import BatchLinks
from .db import LinkDatabase, PageStatus
from .fetcher import LinkFetcher

log = logging.getLogger(__name__)

# Stored status -> what it means. Absent from the store is a state too.
STATE = {
    PageStatus.ARTICLE: "article",
    PageStatus.REDLINK: "redlink",
    None: "unknown",
}


class LinkStore:
    """Holds the links between pages, and retrieves what it does not hold.

    Given a batch of titles it answers from the database, and hands whatever is
    missing to the fetcher. The answer is a mapping either way, so nothing
    upstream learns which pages came off disk and which came off the wire.

    Without a fetcher it answers only from what it holds, which is the case
    when walking a loaded dump.
    """

    def __init__(
        self,
        name: str,
        fetcher: Callable[[str | None], wikifetcher.Fetcher] | None = None,
        *,
        concurrency: int = wikifetcher.MAX_CONCURRENCY,
        max_fetched: int | None = None,
    ) -> None:
        self._max_fetched = max_fetched
        self.fetched = 0

        # Why a title could not be read, when something went wrong reading it.
        # A title absent from here was never attempted.
        self.failures: dict[str, str] = {}
        self._db = LinkDatabase(DB_FILE.get(name, name))
        self._fetcher = (
            LinkFetcher(fetcher(SITE.get(name)), concurrency=concurrency)
            if fetcher is not None
            else None
        )
        self._open: BatchLinks | None = None

        # A fetcher decides which wiki this store holds; a loaded one already
        # knows.
        if self._fetcher is not None:
            self._db.set_meta("site", self._fetcher.site)

    def __enter__(self) -> LinkStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def site(self) -> str | None:
        return self._db.get_meta("site")

    @property
    def budget_spent(self) -> bool:
        """Whether the fetch budget is used up, so nothing more can be read."""
        return self._max_fetched is not None and self.fetched >= self._max_fetched

    def page_count(self) -> int:
        return self._db.page_count()

    def get_links(self, titles: Iterable[str]) -> BatchLinks:
        """Links for these titles. Ones not held yet are retrieved."""
        # Anything still outstanding from the previous batch is worth keeping.
        if self._open is not None:
            self._open.drain()

        wanted = list(dict.fromkeys(titles))
        known = self._db.get_links(wanted)

        pending = {}
        if self._fetcher is not None:
            to_fetch = [t for t in wanted if t not in known]

            # A red link becomes an article only when somebody writes one, so
            # these are worth another look but rarely.
            stale = self._db.stale_titles(
                [t for t, links in known.items() if links is None],
                RED_LINK_TTL_S,
                status=PageStatus.REDLINK,
            )
            to_fetch.extend(stale)

            # Whatever the budget will not cover stays absent, which the search
            # already reads as "could not be read".
            if self._max_fetched is not None:
                allowed = max(0, self._max_fetched - self.fetched)
                if len(to_fetch) > allowed:
                    log.info(
                        "    fetch budget %d reached: %d page(s) left unread",
                        self._max_fetched, len(to_fetch) - allowed,
                    )
                    to_fetch = to_fetch[:allowed]

            log.info(
                "    %sbatch of %d: %d held, %d to retrieve",
                logctx.where(), len(wanted), len(known), len(to_fetch),
            )
            if to_fetch:
                self.fetched += len(to_fetch)
                pending = self._fetcher.submit(to_fetch)

        self._open = BatchLinks(self, known, pending)
        return self._open

    def fetch_page(self, title: str) -> list[str] | None:
        """Retrieve one page now and record what came back.

        For the odd title wanted on its own — a search's endpoints — rather than
        a frontier. Returns its links, or None for no article and for a read
        that failed; `failures` tells those two apart.
        """
        if self._fetcher is None or self.budget_spent:
            return None

        self.fetched += 1
        future = self._fetcher.submit([title])[title]
        try:
            links = future.result(timeout=FETCH_TIMEOUT_S)
        except TimeoutError:
            self.note_failure(title, f"timed out after {FETCH_TIMEOUT_S:.0f}s")
            return None
        except Exception as exc:
            self.note_failure(title, f"{type(exc).__name__}: {exc}")
            return None

        self.write(title, links)
        return links

    def status(self, title: str) -> PageStatus | None:
        """The title's status, or None if it is unknown — retrieving first."""
        known = self._db.status(title)
        if known is None and self._fetcher is not None:
            self.fetch_page(title)
            known = self._db.status(title)
        return known

    def state(self, title: str) -> str:
        """What is known about a title: article, redlink, or unknown.

        All three are properties of the page itself, so none of them change
        when some other page does.
        """
        return STATE[self.status(title)]

    def note_failure(self, title: str, reason: str) -> None:
        """Record why a retrieval failed, so a caller can say more than 'unknown'."""
        self.failures[title] = reason

    def write(self, title: str, links: list[str] | None) -> None:
        """Record what a retrieval found. Called by the batch as pages land."""
        if links is None:
            self.mark_red_links([title])
            log.info("      no article at: %s", title)
        else:
            self.store(title, links)
            log.debug("      store %s (%d link(s))", title, len(links))

    def store(self, title: str, links: list[str]) -> None:
        self._db.store(title, links)

    def bulk_write(self, pages: Iterable[tuple[str, list[str] | None]]) -> tuple[int, int]:
        """Record many pages at once. `None` links mean no article exists."""
        return self._db.bulk_write(pages)

    def forget(self, title: str) -> tuple[int, int]:
        """Drop a title from the store so the next walk retrieves it again."""
        return self._db.forget(title)

    def mark_red_links(self, titles: Iterable[str]) -> None:
        self._db.mark_red_links(titles)

    def link_count(self) -> int:
        return self._db.link_count()

    def red_link_count(self) -> int:
        return self._db.red_link_count()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self._db.get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self._db.set_meta(key, value)

    def close(self) -> None:
        """Keep the pages that landed; do not wait for the ones queued behind
        them. The walk is over — by an answer, a budget or a Ctrl-C — so a page
        still waiting its turn has no reader left."""
        if self._open is not None:
            self._open.keep_what_landed()
        if self._fetcher is not None:
            self._fetcher.close()
        self._db.close()
