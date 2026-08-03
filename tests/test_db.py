"""Tests for the storage layer.

The central property is the three-way distinction the schema exists to express:

    fetched, links nowhere   ->  present in get_links, empty list
    no such article          ->  absent from get_links, present in red_links
    never fetched            ->  absent from both

Collapsing any two of those is the bug this module is built to prevent, so most
of what follows is checking they stay apart.
"""

from __future__ import annotations

from link_store import RED_LINK_TTL_S, LinkDatabase


# --------------------------------------------------------------------------
# The three-way distinction
# --------------------------------------------------------------------------

def test_fetched_article_with_no_links_is_present_and_empty(db: LinkDatabase) -> None:
    db.store("Barren", [])

    assert db.get_links(["Barren"]) == {"Barren": []}


def test_red_link_is_absent_from_get_links(db: LinkDatabase) -> None:
    """A red link must not look like an article that happens to link nowhere.

    Before the status filter existed this returned `{"Nowhere": []}`, and the
    walker counted a non-existent page as a fully explored one.
    """
    db.mark_red_links(["Nowhere"])

    assert db.get_links(["Nowhere"]) == {}
    assert db.red_links(["Nowhere"]) == {"Nowhere"}


def test_never_fetched_title_is_absent_from_both(db: LinkDatabase) -> None:
    assert db.get_links(["Unfetched"]) == {}
    assert db.red_links(["Unfetched"]) == set()


def test_the_three_kinds_are_told_apart_in_one_call(db: LinkDatabase) -> None:
    db.store("Barren", [])
    db.mark_red_links(["Nowhere"])
    titles = ["Barren", "Nowhere", "Unfetched"]

    articles = db.get_links(titles)
    dead = db.red_links(titles)

    assert set(articles) == {"Barren"}
    assert dead == {"Nowhere"}
    assert [t for t in titles if t not in articles and t not in dead] == ["Unfetched"]


def test_red_links_does_not_report_ordinary_articles(db: LinkDatabase) -> None:
    db.store("Real", ["Elsewhere"])

    assert db.red_links(["Real"]) == set()


def test_status_reports_all_three_kinds(db: LinkDatabase) -> None:
    db.store("Barren", [])
    db.mark_red_links(["Nowhere"])

    assert db.status("Barren") == "ok"
    assert db.status("Nowhere") == "redlink"
    assert db.status("Unfetched") is None


# --------------------------------------------------------------------------
# Recording a red link over existing data
# --------------------------------------------------------------------------

def test_marking_a_red_link_removes_a_deleted_articles_edges(db: LinkDatabase) -> None:
    """An article that later 404s must lose its edges, not keep them.

    Otherwise the search keeps walking out of a page that no longer exists.
    """
    db.store("Doomed", ["A", "B"])

    db.mark_red_links(["Doomed"])

    assert db.get_links(["Doomed"]) == {}
    assert db.red_links(["Doomed"]) == {"Doomed"}
    assert db.link_count() == 0


def test_storing_an_article_clears_a_previous_red_link(db: LinkDatabase) -> None:
    """The reverse direction: somebody wrote the missing article."""
    db.mark_red_links(["Someday"])

    db.store("Someday", ["Bristol"])

    assert db.get_links(["Someday"]) == {"Someday": ["Bristol"]}
    assert db.red_links(["Someday"]) == set()


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def test_counts_separate_articles_from_red_links(db: LinkDatabase) -> None:
    db.store("One", ["Two"])
    db.store("Two", [])
    db.mark_red_links(["Nowhere", "Neither"])

    assert db.page_count() == 2
    assert db.red_link_count() == 2
    assert db.link_count() == 1


# --------------------------------------------------------------------------
# Link storage
# --------------------------------------------------------------------------

def test_links_come_back_in_page_order(db: LinkDatabase) -> None:
    """Order is stable so two runs over the same data return the same path."""
    order = ["Zebra", "Apple", "Mango"]
    db.store("Page", order)

    assert db.get_links(["Page"])["Page"] == order


def test_restoring_a_page_replaces_its_links_rather_than_appending(db: LinkDatabase) -> None:
    """A refresh must reflect removals, not just additions."""
    db.store("Page", ["Old", "Kept"])

    db.store("Page", ["Kept", "New"])

    assert db.get_links(["Page"])["Page"] == ["Kept", "New"]
    assert db.link_count() == 2


def test_a_page_can_link_to_the_same_title_twice(db: LinkDatabase) -> None:
    """Duplicates survive storage — `ord` is the key, not `dst`.

    Real pages do this (an infobox and the body linking the same article), and
    the dump produces it too when two redirects resolve to one target.
    """
    db.store("Page", ["Same", "Other", "Same"])

    assert db.get_links(["Page"])["Page"] == ["Same", "Other", "Same"]


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------

def test_nothing_is_stale_within_the_age_limit(db: LinkDatabase) -> None:
    db.store("Fresh", [])

    assert db.stale_titles(["Fresh"], max_age_s=RED_LINK_TTL_S) == []


def test_everything_known_is_stale_at_a_zero_age_limit(db: LinkDatabase) -> None:
    db.store("Fresh", [])

    assert db.stale_titles(["Fresh"], max_age_s=-1) == ["Fresh"]


def test_unknown_titles_are_never_reported_stale(db: LinkDatabase) -> None:
    """Stale means "known and old". A title we have never seen is neither."""
    assert db.stale_titles(["Unfetched"], max_age_s=-1) == []


def test_staleness_can_be_narrowed_to_red_links(db: LinkDatabase) -> None:
    """Articles and red links get different lifetimes, so the check must
    be able to look at one kind without dragging in the other."""
    db.store("Article", [])
    db.mark_red_links(["Nowhere"])

    stale = db.stale_titles(["Article", "Nowhere"], max_age_s=-1, status="redlink")

    assert stale == ["Nowhere"]


# --------------------------------------------------------------------------
# The host-parameter limit
# --------------------------------------------------------------------------

def test_queries_survive_more_titles_than_sqlite_allows_per_statement() -> None:
    """A BFS frontier is routinely larger than SQLite's parameter cap.

    Every `IN (...)` query batches below the limit; this exercises the seam by
    asking for more titles than one statement could ever carry. Without the
    batching this raises `sqlite3.OperationalError: too many SQL variables`.
    """
    with LinkDatabase(":memory:") as db:
        articles = [f"Article_{i:04d}" for i in range(1500)]
        dead = [f"Missing_{i:04d}" for i in range(1500)]
        for title in articles:
            db.store(title, ["Hub"])
        db.mark_red_links(dead)

        wanted = articles + dead
        assert len(db.get_links(wanted)) == len(articles)
        assert len(db.red_links(wanted)) == len(dead)
        assert len(db.stale_titles(wanted, max_age_s=-1)) == len(wanted)


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------

def test_meta_round_trips_and_defaults(db: LinkDatabase) -> None:
    assert db.get_meta("site", "fallback.example") == "fallback.example"

    db.set_meta("site", "simple.wikipedia.org")

    assert db.get_meta("site") == "simple.wikipedia.org"


def test_meta_overwrites_rather_than_duplicating(db: LinkDatabase) -> None:
    db.set_meta("site", "first.example")
    db.set_meta("site", "second.example")

    assert db.get_meta("site") == "second.example"
