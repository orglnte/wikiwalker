"""Tests for the storage layer.

The central property is the three-way distinction the schema exists to express:

    fetched, links nowhere   ->  present in get_links, empty list
    no such article          ->  absent from get_links, present in red_links
    never fetched            ->  absent from both

Collapsing any two of those is the bug this module is built to prevent, so most
of what follows is checking they stay apart.
"""

from __future__ import annotations

from link_store import LinkDatabase
from settings import RED_LINK_TTL_S

# --------------------------------------------------------------------------
# The three-way distinction
# --------------------------------------------------------------------------

def test_fetched_article_with_no_links_is_present_and_empty(sql: LinkDatabase) -> None:
    sql.store("Barren", [])

    assert sql.get_links(["Barren"]) == {"Barren": []}


def test_a_red_link_reads_as_no_article_not_as_empty(sql: LinkDatabase) -> None:
    """None and [] must not be confused: no article, versus an article that
    links nowhere."""
    sql.mark_red_links(["Nowhere"])

    assert sql.get_links(["Nowhere"]) == {"Nowhere": None}


def test_never_fetched_title_is_absent(sql: LinkDatabase) -> None:
    assert sql.get_links(["Unfetched"]) == {}


def test_the_three_kinds_are_told_apart_in_one_call(sql: LinkDatabase) -> None:
    sql.store("Barren", [])
    sql.mark_red_links(["Nowhere"])

    known = sql.get_links(["Barren", "Nowhere", "Unfetched"])

    assert known == {"Barren": [], "Nowhere": None}
    assert "Unfetched" not in known


def test_red_links_does_not_report_ordinary_articles(sql: LinkDatabase) -> None:
    sql.store("Real", ["Elsewhere"])

    assert sql.red_links(["Real"]) == set()


def test_status_reports_all_three_kinds(sql: LinkDatabase) -> None:
    sql.store("Barren", [])
    sql.mark_red_links(["Nowhere"])

    assert sql.status("Barren") == "ok"
    assert sql.status("Nowhere") == "redlink"
    assert sql.status("Unfetched") is None


# --------------------------------------------------------------------------
# Recording a red link over existing data
# --------------------------------------------------------------------------

def test_marking_a_red_link_removes_a_deleted_articles_edges(sql: LinkDatabase) -> None:
    """An article that later 404s must lose its edges, not keep them.

    Otherwise the search keeps walking out of a page that no longer exists.
    """
    sql.store("Doomed", ["A", "B"])

    sql.mark_red_links(["Doomed"])

    assert sql.get_links(["Doomed"]) == {"Doomed": None}
    assert sql.red_links(["Doomed"]) == {"Doomed"}
    assert sql.link_count() == 0


def test_storing_an_article_clears_a_previous_red_link(sql: LinkDatabase) -> None:
    """The reverse direction: somebody wrote the missing article."""
    sql.mark_red_links(["Someday"])

    sql.store("Someday", ["Bristol"])

    assert sql.get_links(["Someday"]) == {"Someday": ["Bristol"]}
    assert sql.red_links(["Someday"]) == set()


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def test_counts_separate_articles_from_red_links(sql: LinkDatabase) -> None:
    sql.store("One", ["Two"])
    sql.store("Two", [])
    sql.mark_red_links(["Nowhere", "Neither"])

    assert sql.page_count() == 2
    assert sql.red_link_count() == 2
    assert sql.link_count() == 1


# --------------------------------------------------------------------------
# Link storage
# --------------------------------------------------------------------------

def test_links_come_back_in_page_order(sql: LinkDatabase) -> None:
    """Order is stable so two runs over the same data return the same path."""
    order = ["Zebra", "Apple", "Mango"]
    sql.store("Page", order)

    assert sql.get_links(["Page"])["Page"] == order


def test_restoring_a_page_replaces_its_links_rather_than_appending(sql: LinkDatabase) -> None:
    """A refresh must reflect removals, not just additions."""
    sql.store("Page", ["Old", "Kept"])

    sql.store("Page", ["Kept", "New"])

    assert sql.get_links(["Page"])["Page"] == ["Kept", "New"]
    assert sql.link_count() == 2


def test_a_page_can_link_to_the_same_title_twice(sql: LinkDatabase) -> None:
    """Duplicates survive storage — `ord` is the key, not `dst`.

    Real pages do this (an infobox and the body linking the same article), and
    the dump produces it too when two redirects resolve to one target.
    """
    sql.store("Page", ["Same", "Other", "Same"])

    assert sql.get_links(["Page"])["Page"] == ["Same", "Other", "Same"]


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------

def test_nothing_is_stale_within_the_age_limit(sql: LinkDatabase) -> None:
    sql.store("Fresh", [])

    assert sql.stale_titles(["Fresh"], max_age_s=RED_LINK_TTL_S) == []


def test_everything_known_is_stale_at_a_zero_age_limit(sql: LinkDatabase) -> None:
    sql.store("Fresh", [])

    assert sql.stale_titles(["Fresh"], max_age_s=-1) == ["Fresh"]


def test_unknown_titles_are_never_reported_stale(sql: LinkDatabase) -> None:
    """Stale means "known and old". A title we have never seen is neither."""
    assert sql.stale_titles(["Unfetched"], max_age_s=-1) == []


def test_staleness_can_be_narrowed_to_red_links(sql: LinkDatabase) -> None:
    """Articles and red links get different lifetimes, so the check must
    be able to look at one kind without dragging in the other."""
    sql.store("Article", [])
    sql.mark_red_links(["Nowhere"])

    stale = sql.stale_titles(["Article", "Nowhere"], max_age_s=-1, status="redlink")

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
    with LinkDatabase(":memory:") as sql:
        articles = [f"Article_{i:04d}" for i in range(1500)]
        dead = [f"Missing_{i:04d}" for i in range(1500)]
        for title in articles:
            sql.store(title, ["Hub"])
        sql.mark_red_links(dead)

        wanted = articles + dead
        known = sql.get_links(wanted)
        assert len(known) == len(wanted)
        assert sum(1 for links in known.values() if links is None) == len(dead)
        assert len(sql.red_links(wanted)) == len(dead)
        assert len(sql.stale_titles(wanted, max_age_s=-1)) == len(wanted)


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------

def test_meta_round_trips_and_defaults(sql: LinkDatabase) -> None:
    assert sql.get_meta("site", "fallback.example") == "fallback.example"

    sql.set_meta("site", "simple.wikipedia.org")

    assert sql.get_meta("site") == "simple.wikipedia.org"


def test_meta_overwrites_rather_than_duplicating(sql: LinkDatabase) -> None:
    sql.set_meta("site", "first.example")
    sql.set_meta("site", "second.example")

    assert sql.get_meta("site") == "second.example"
