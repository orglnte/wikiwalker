"""The store of links a Walker walks."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

import wikifetcher
from settings import DB_FILE, FETCH_TIMEOUT_S, SITE

from .batch import MISSING, PAGE_FETCH_FAILED, BatchLinks, Entry
from .db import LinkDatabase, PageType
from .fetcher import LinkFetcher

log = logging.getLogger(__name__)

# Stored type -> what it means. Absent from the store is a state too.
STATE = {
    PageType.ARTICLE: "article",
    PageType.NOTFOUND: "notfound",
    None: "unknown",
}


class LinkStore:
    """Holds the links between pages, and retrieves what it does not hold.

    Given a batch of titles it answers from the database, and hands whatever is
    absent to the fetcher. The answer is a mapping either way, so nothing
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
        self._batchlinks: BatchLinks | None = None

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

    def open_batch(self, titles: Iterable[str]) -> None:
        """Read what is held for these titles and start retrieving the rest."""
        # Anything still outstanding from the previous batch is worth keeping.
        if self._batchlinks is not None:
            self._absorb(self._batchlinks.drain())

        allowance = (
            None if self._max_fetched is None
            else max(0, self._max_fetched - self.fetched)
        )
        self._batchlinks = BatchLinks(titles, self._db, self._fetcher, allowance)
        self.fetched += self._batchlinks.submitted

    def links_of(self, title: str) -> list[str] | None:
        """The title's links. Blocks while it is still being retrieved.

        None means no article behind the title. PAGE_FETCH_FAILED means the
        store has no answer, which is what makes a walk non-exhaustive.
        """
        entry = self._batchlinks.get(title)

        match entry.arrived:
            case wikifetcher.Page() as page:
                self.write(page)
                return page.links
            case wikifetcher.PageFetchFailed(reason=reason):
                self.failures[title] = reason
                return PAGE_FETCH_FAILED

        if entry.held is MISSING:
            # No reason recorded: every title in a spent batch has the same one,
            # and only the two endpoints' reasons are ever read.
            return PAGE_FETCH_FAILED

        return entry.held

    def _absorb(self, entries: dict[str, Entry]) -> None:
        """Keep what a batch resolved that nobody read."""
        for title, entry in entries.items():
            match entry.arrived:
                case wikifetcher.Page() as page:
                    self.write(page)
                case wikifetcher.PageFetchFailed(reason=reason):
                    self.failures[title] = reason

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
            page = future.result(timeout=FETCH_TIMEOUT_S)
        except TimeoutError:
            self.note_failure(title, f"timed out after {FETCH_TIMEOUT_S:.0f}s")
            return None
        except Exception as exc:
            self.note_failure(title, f"{type(exc).__name__}: {exc}")
            return None

        self.write(page)
        return page.links

    def page_type(self, title: str) -> PageType | None:
        """The title's type, or None if it is unknown — retrieving first.

        A redirect answers with its destination's type: it names that page,
        and no caller outside this module has any use for the difference.
        """
        known = self._db.page_type(title)
        if known is None and self._fetcher is not None:
            self.fetch_page(title)
            known = self._db.page_type(title)

        if known == PageType.REDIRECT:
            destination = self._db.destination(title)
            known = self._db.page_type(destination) if destination else None
        return known

    def state(self, title: str) -> str:
        """What is known about a title: article, notfound, or unknown.

        All three are properties of the page itself, so none of them change
        when some other page does. Whether a link to it is red depends on the
        pages that link to it, so it is not one of these.
        """
        return STATE[self.page_type(title)]

    def destination(self, title: str) -> str | None:
        """What this title redirects to, or None if it does not."""
        return self._db.destination(title)

    def note_failure(self, title: str, reason: str) -> None:
        """Record why a retrieval failed, so a caller can say more than 'unknown'."""
        self.failures[title] = reason

    def write(self, page: wikifetcher.Page) -> None:
        """Record what a retrieval found. Called by the batch as pages land.

        A page is filed under the title the fetch landed on, and the titles
        that led there are recorded as pointing at it — so a redirect costs one
        retrieval rather than one per name the article answers to.
        """
        if page.links is None:
            self.mark_not_found([page.title])
            log.info("      no article at: %s", page.title)
        else:
            self.store(page.title, page.links)
            log.debug("      store %s (%d link(s))", page.title, len(page.links))

        if page.aliases:
            self._db.mark_redirects(page.aliases, page.title)
            log.info("      %s redirects to %s", ", ".join(page.aliases), page.title)

    def store(self, title: str, links: list[str]) -> None:
        self._db.store(title, links)

    def bulk_write(self, pages: Iterable[tuple[str, list[str] | None]]) -> tuple[int, int]:
        """Record many pages at once. `None` links mean no article exists."""
        return self._db.bulk_write(pages)

    def forget(self, title: str) -> tuple[int, int]:
        """Drop a title from the store so the next walk retrieves it again."""
        return self._db.forget(title)

    def mark_not_found(self, titles: Iterable[str]) -> None:
        self._db.mark_not_found(titles)

    def link_count(self) -> int:
        return self._db.link_count()

    def not_found_count(self) -> int:
        return self._db.not_found_count()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self._db.get_meta(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self._db.set_meta(key, value)

    def close(self) -> None:
        """Keep the pages that landed; do not wait for the ones queued behind
        them. The walk is over — by an answer, a budget or a Ctrl-C — so a page
        still waiting its turn has no reader left."""
        if self._batchlinks is not None:
            self._absorb(self._batchlinks.keep_what_landed())
        if self._fetcher is not None:
            self._fetcher.close()
        self._db.close()
