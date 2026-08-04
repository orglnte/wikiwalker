"""Tests for a store that fetches what it does not hold.

Covers the path a crawl takes: a miss becomes a future, the future resolves
into the store, and the search sees a mapping either way. What matters is that
the three outcomes stay apart — links, no article, could not read — because the
last one is the only one that makes a walk non-exhaustive.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future

import pytest

from link_store import BatchLinks, LinkFetcher, LinkStore
from walker import Walker


class FakeFetcher:
    """Serves a fixed graph. Titles in `broken` raise instead of answering."""

    site = "test.invalid"

    def __init__(self, pages, *, broken=(), delay_s=0.0):
        self._pages = pages
        self._broken = set(broken)
        self._delay_s = delay_s
        self.requested: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def fetch(self, title):
        self.requested.append(title)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            if title in self._broken:
                raise RuntimeError(f"cannot read {title}")
            return self._pages.get(title)
        finally:
            self.in_flight -= 1


def serving(pages, **kwargs):
    """A fetcher factory of the shape LinkStore constructs."""
    made = {}

    def factory(site=None):
        made["fetcher"] = FakeFetcher(pages, **kwargs)
        return made["fetcher"]

    factory.made = made
    return factory


# --------------------------------------------------------------------------
# Retrieving a miss
# --------------------------------------------------------------------------

def test_a_page_the_store_lacks_is_fetched_and_kept() -> None:
    factory = serving({"A": ["B"]})
    with LinkStore(":memory:", factory) as store:
        assert store.get_links(["A"]).get("A") == ["B"]
        # asking again must not go out a second time
        assert store.get_links(["A"]).get("A") == ["B"]

    assert factory.made["fetcher"].requested == ["A"]


def test_a_page_already_held_is_not_fetched() -> None:
    factory = serving({"A": ["B"]})
    with LinkStore(":memory:", factory) as store:
        store.store("A", ["B"])

        assert store.get_links(["A"]).get("A") == ["B"]

    assert factory.made["fetcher"].requested == []


def test_a_title_with_no_article_becomes_a_red_link() -> None:
    with LinkStore(":memory:", serving({})) as store:
        assert store.get_links(["Nowhere"]).get("Nowhere") is None
        assert store.red_link_count() == 1
        assert store.status("Nowhere") == "redlink"


def test_a_failed_fetch_is_not_recorded_as_anything() -> None:
    """It must stay unknown so the next walk retries it — and so the current
    walk reports itself as non-exhaustive rather than claiming a dead end."""
    with LinkStore(":memory:", serving({"A": ["B"]}, broken=["A"])) as store:
        links = store.get_links(["A"])

        assert "A" not in links
        assert store.status("A") is None
        assert store.red_link_count() == 0


# --------------------------------------------------------------------------
# The batch
# --------------------------------------------------------------------------

def test_pages_fetched_but_never_asked_for_are_still_kept() -> None:
    """A walk that ends early has already paid for those fetches."""
    with LinkStore(":memory:", serving({"A": ["X"], "B": ["Y"], "C": ["Z"]})) as store:
        batch = store.get_links(["A", "B", "C"])
        batch.get("A")            # only one is ever read
        batch.drain()

        assert store.link_count() == 3


def test_closing_keeps_the_pages_that_landed_and_drops_the_queued() -> None:
    """A batch submits every title at once and a semaphore holds most of them
    back. Waiting on those would send requests the walk will never read."""
    landed: Future = Future()
    landed.set_result(["X"])
    queued: Future = Future()

    with LinkStore(":memory:") as store:
        BatchLinks(store, {}, {"Landed": landed, "Queued": queued}).keep_what_landed()

        assert store.get_links(["Landed"]).get("Landed") == ["X"]
        assert store.status("Queued") is None
        assert queued.cancelled()


def test_a_batch_mixes_held_and_retrieved_pages() -> None:
    with LinkStore(":memory:", serving({"B": ["Y"]})) as store:
        store.store("A", ["X"])

        links = store.get_links(["A", "B"])

        assert links.get("A") == ["X"]
        assert links.get("B") == ["Y"]


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------

def test_fetches_run_concurrently_up_to_the_cap() -> None:
    pages = {f"P{i}": [] for i in range(20)}
    fetcher = FakeFetcher(pages, delay_s=0.05)
    link_fetcher = LinkFetcher(fetcher, concurrency=5)

    started = time.monotonic()
    for future in link_fetcher.submit(sorted(pages)).values():
        future.result()
    elapsed = time.monotonic() - started
    link_fetcher.close()

    assert fetcher.peak <= 5
    assert elapsed < 20 * 0.05 / 2      # serial would be 1.0s; four rounds is ~0.2s


def test_closing_the_fetcher_stops_its_thread() -> None:
    link_fetcher = LinkFetcher(FakeFetcher({}))
    link_fetcher.close()

    assert not link_fetcher._thread.is_alive()


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

def test_a_walk_over_an_empty_store_crawls_its_way_to_the_target() -> None:
    pages = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": ["Target"], "Target": []}

    with LinkStore(":memory:", serving(pages)) as store:
        result = Walker(store).find_path("A", "Target")

    assert result.path == ["A", "B", "D", "Target"]
    assert result.complete


def test_a_page_that_cannot_be_read_makes_the_walk_non_exhaustive() -> None:
    pages = {"A": ["B", "C"], "B": [], "C": ["Target"], "Target": []}

    with LinkStore(":memory:", serving(pages, broken=["C"])) as store:
        result = Walker(store).find_path("A", "Target")

    assert result.path is None
    assert "C" in result.unread
    assert not result.complete


@pytest.mark.parametrize("budget", [1, 2])
def test_a_walk_budget_stops_the_search(budget: int) -> None:
    pages = {f"P{i}": [f"P{i + 1}"] for i in range(20)}

    with LinkStore(":memory:", serving(pages)) as store:
        result = Walker(store, batch_size=1).find_path("P0", "P19", max_walked=budget)

    assert result.path is None
    assert not result.complete
    assert result.pages_expanded <= budget


@pytest.mark.parametrize("budget", [1, 3])
def test_a_fetch_budget_stops_retrieval(budget: int) -> None:
    """The walk carries on over what is held; the rest is unread, so the
    result reports itself as non-exhaustive rather than as a dead end."""
    pages = {f"P{i}": [f"P{i + 1}"] for i in range(20)}
    factory = serving(pages)

    with LinkStore(":memory:", factory, max_fetched=budget) as store:
        result = Walker(store, batch_size=100).find_path("P0", "P19")

    assert len(factory.made["fetcher"].requested) <= budget
    assert not result.complete


def test_a_fetch_budget_does_not_bound_walking() -> None:
    """Pages already held cost nothing to walk, so they stay walkable after
    the budget is spent."""
    factory = serving({})
    with LinkStore(":memory:", factory, max_fetched=0) as store:
        store.store("A", ["B"])
        store.store("B", [])

        result = Walker(store).find_path("A", "B")

    assert result.path == ["A", "B"]
    assert factory.made["fetcher"].requested == []


# --------------------------------------------------------------------------
# Forgetting
# --------------------------------------------------------------------------

def test_a_forgotten_page_is_retrieved_again() -> None:
    """What makes the read-through visible: the same walk answers from the
    store, then from the fetcher once the page is dropped."""
    factory = serving({"A": ["B"], "B": []})

    with LinkStore(":memory:", factory) as store:
        store.bulk_write([("A", ["B"]), ("B", [])])
        assert store.get_links(["A"]).get("A") == ["B"]
        assert factory.made["fetcher"].requested == []

        store.forget("A")

        assert store.get_links(["A"]).get("A") == ["B"]
        assert factory.made["fetcher"].requested == ["A"]


def test_a_walk_over_a_hole_is_not_exhaustive() -> None:
    """Forgetting a page a walk needs makes the result honest about it."""
    with LinkStore(":memory:") as store:
        store.bulk_write([("A", ["B"]), ("B", ["Target"]), ("Target", [])])
        assert Walker(store).find_path("A", "Target").complete

        store.forget("B")
        result = Walker(store).find_path("A", "Target")

    assert result.path is None
    assert result.unread == {"B"}
    assert not result.complete
