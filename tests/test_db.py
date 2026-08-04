"""Tests for the storage layer.

The central property is the three-way distinction the schema exists to express:

    fetched, links nowhere   ->  present in get_links, empty list
    no such article          ->  absent from get_links, present in not_found
    never fetched            ->  absent from both

Collapsing any two of those is the bug this module is built to prevent, so most
of what follows is checking they stay apart.
"""

from __future__ import annotations

import sqlite3

import pytest

from link_store import LinkDatabase
from settings import NOT_FOUND_TTL_S

# --------------------------------------------------------------------------
# The three-way distinction
# --------------------------------------------------------------------------

def test_fetched_article_with_no_links_is_present_and_empty(sql: LinkDatabase) -> None:
    sql.store("Barren", [])

    assert sql.get_links(["Barren"]) == {"Barren": []}


def test_a_missing_title_reads_as_no_article_not_as_empty(sql: LinkDatabase) -> None:
    """None and [] must not be confused: no article, versus an article that
    links nowhere."""
    sql.mark_not_found(["Nowhere"])

    assert sql.get_links(["Nowhere"]) == {"Nowhere": None}


def test_never_fetched_title_is_absent(sql: LinkDatabase) -> None:
    assert sql.get_links(["Unfetched"]) == {}


def test_the_three_kinds_are_told_apart_in_one_call(sql: LinkDatabase) -> None:
    sql.store("Barren", [])
    sql.mark_not_found(["Nowhere"])

    known = sql.get_links(["Barren", "Nowhere", "Unfetched"])

    assert known == {"Barren": [], "Nowhere": None}
    assert "Unfetched" not in known


def test_not_found_does_not_report_ordinary_articles(sql: LinkDatabase) -> None:
    sql.store("Real", ["Elsewhere"])

    assert sql.not_found(["Real"]) == set()


def test_a_row_cannot_carry_a_type_outside_the_three(sql: LinkDatabase) -> None:
    """`unknown` is the absence of a row, so it must not be storable as one."""
    for bad in ("unknown", "hello there"):
        with pytest.raises(sqlite3.IntegrityError):
            sql._conn.execute(
                "INSERT INTO pages (title, fetched_at, type) VALUES (?, ?, ?)",
                ("Somewhere", 0.0, bad),
            )


def test_page_type_reports_all_three_kinds(sql: LinkDatabase) -> None:
    sql.store("Barren", [])
    sql.mark_not_found(["Nowhere"])

    assert sql.page_type("Barren") == "article"
    assert sql.page_type("Nowhere") == "notfound"
    assert sql.page_type("Unfetched") is None


# --------------------------------------------------------------------------
# Recording a 404 over existing data
# --------------------------------------------------------------------------

def test_marking_a_title_not_found_removes_its_links(sql: LinkDatabase) -> None:
    """An article that later 404s must lose its edges, not keep them.

    Otherwise the search keeps walking out of a page that no longer exists.
    """
    sql.store("Doomed", ["A", "B"])

    sql.mark_not_found(["Doomed"])

    assert sql.get_links(["Doomed"]) == {"Doomed": None}
    assert sql.not_found(["Doomed"]) == {"Doomed"}
    assert sql.link_count() == 0


def test_storing_an_article_clears_a_previous_404(sql: LinkDatabase) -> None:
    """The reverse direction: somebody wrote the missing article."""
    sql.mark_not_found(["Someday"])

    sql.store("Someday", ["Bristol"])

    assert sql.get_links(["Someday"]) == {"Someday": ["Bristol"]}
    assert sql.not_found(["Someday"]) == set()


# --------------------------------------------------------------------------
# Building from a dump
# --------------------------------------------------------------------------

STAGING = """
CREATE TABLE t_page (page_id, page_title, page_namespace, page_is_redirect);
CREATE TABLE t_pagelinks (pl_from, pl_target_id, pl_from_namespace);
CREATE TABLE t_redirect (rd_from, rd_namespace, rd_title);
CREATE TABLE t_target (target_id, title);

INSERT INTO t_page VALUES (1, 'Bristol', '0', '0'), (2, 'Cheese', '0', '0'),
                          (3, 'Cheddar', '0', '1');
INSERT INTO t_pagelinks VALUES (1, 10, '0'), (1, 11, '0'), (1, 12, '0');
INSERT INTO t_redirect VALUES (3, '0', 'Cheese');
INSERT INTO t_target VALUES (10, 'Cheese'), (11, 'Nowhere'), (12, 'Cheddar');
"""


def test_a_dump_load_separates_articles_from_the_titles_they_link_to(
    sql: LinkDatabase,
) -> None:
    """A snapshot is complete by construction, so a title with no page of its
    own is recorded as missing rather than as something still to look at."""
    sql._conn.executescript(STAGING)

    sql.build_from_staging("0")

    assert sql.get_links(["Bristol", "Cheese", "Nowhere"]) == {
        "Bristol": ["Cheddar", "Cheese", "Nowhere"],
        "Cheese": [],
        "Nowhere": None,
    }
    assert sql.page_count() == 2
    assert sql.not_found_count() == 1


def test_a_dump_load_keeps_redirects_as_titles_naming_a_page(
    sql: LinkDatabase,
) -> None:
    """The dump carries them; resolving them away is what made one page look
    like two edges when something linked to both names."""
    sql._conn.executescript(STAGING)

    sql.build_from_staging("0")

    assert sql.page_type("Cheddar") == "redirect"
    assert sql.destination("Cheddar") == "Cheese"
    assert sql.get_links(["Cheddar"]) == {"Cheddar": []}


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------

def test_a_redirect_answers_with_the_article_it_names(sql: LinkDatabase) -> None:
    """And without costing a step: a link to a redirect lands on the article
    in one click, so counting it would make paths through one come out long."""
    sql.store("United_Kingdom", ["London", "Wales"])
    sql.mark_redirects(["UK"], "United_Kingdom")

    assert sql.get_links(["UK"]) == {"UK": ["London", "Wales"]}


def test_a_redirect_to_a_redirect_stops_at_the_second(sql: LinkDatabase) -> None:
    """A wiki serves the first destination rather than following on, so the
    second redirect is a page holding a single link — one more click."""
    sql.store("Cheese", ["Milk"])
    sql.mark_redirects(["Cheddar"], "Cheese")
    sql.mark_redirects(["Chedder"], "Cheddar")

    # Chedder lands on Cheddar's page, which offers one link onwards.
    assert sql.get_links(["Chedder"]) == {"Chedder": ["Cheese"]}
    assert sql.get_links(["Cheddar"]) == {"Cheddar": ["Milk"]}


def test_a_redirect_to_something_unread_reads_as_unread(sql: LinkDatabase) -> None:
    sql.mark_redirects(["UK"], "United_Kingdom")

    assert sql.get_links(["UK"]) == {}


def test_marking_a_redirect_drops_the_edges_it_had(sql: LinkDatabase) -> None:
    """A title that was an article and is now a redirect must not keep links
    of its own, or the search walks out of a page that no longer has them."""
    sql.store("UK", ["Somewhere"])

    sql.mark_redirects(["UK"], "United_Kingdom")

    assert sql.page_type("UK") == "redirect"
    assert sql.destination("UK") == "United_Kingdom"
    assert sql.link_count() == 0


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def test_counts_separate_articles_from_missing_titles(sql: LinkDatabase) -> None:
    sql.store("One", ["Two"])
    sql.store("Two", [])
    sql.mark_not_found(["Nowhere", "Neither"])

    assert sql.page_count() == 2
    assert sql.not_found_count() == 2
    assert sql.link_count() == 1


# --------------------------------------------------------------------------
# Link storage
# --------------------------------------------------------------------------

def test_links_come_back_sorted(sql: LinkDatabase) -> None:
    """A dump has no page positions to give, only a set of links, so both ways
    of filling the store sort them — otherwise the same wiki read two ways
    answers with different paths of the same length."""
    sql.store("Page", ["Zebra", "Apple", "Mango"])

    assert sql.get_links(["Page"])["Page"] == ["Apple", "Mango", "Zebra"]


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

    assert sql.get_links(["Page"])["Page"] == ["Other", "Same", "Same"]


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------

def test_nothing_is_stale_within_the_age_limit(sql: LinkDatabase) -> None:
    sql.store("Fresh", [])

    assert sql.stale_titles(["Fresh"], max_age_s=NOT_FOUND_TTL_S) == []


def test_everything_known_is_stale_at_a_zero_age_limit(sql: LinkDatabase) -> None:
    sql.store("Fresh", [])

    assert sql.stale_titles(["Fresh"], max_age_s=-1) == ["Fresh"]


def test_unknown_titles_are_never_reported_stale(sql: LinkDatabase) -> None:
    """Stale means "known and old". A title we have never seen is neither."""
    assert sql.stale_titles(["Unfetched"], max_age_s=-1) == []


def test_staleness_can_be_narrowed_to_missing_titles(sql: LinkDatabase) -> None:
    """An article and a title that 404s get different lifetimes, so the
    be able to look at one kind without dragging in the other."""
    sql.store("Article", [])
    sql.mark_not_found(["Nowhere"])

    stale = sql.stale_titles(["Article", "Nowhere"], max_age_s=-1, page_type="notfound")

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
        sql.mark_not_found(dead)

        wanted = articles + dead
        known = sql.get_links(wanted)
        assert len(known) == len(wanted)
        assert sum(1 for links in known.values() if links is None) == len(dead)
        assert len(sql.not_found(wanted)) == len(dead)
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


# --------------------------------------------------------------------------
# Forgetting
# --------------------------------------------------------------------------

def test_forgetting_a_page_returns_it_to_unknown(sql: LinkDatabase) -> None:
    """Not the same as marking it a red link: that says "no article exists",
    this says nothing at all, so the next walk fetches it."""
    sql.store("Cheshire", ["Cheese", "England"])

    pages, links = sql.forget("Cheshire")

    assert (pages, links) == (1, 2)
    assert sql.page_type("Cheshire") is None
    assert sql.get_links(["Cheshire"]) == {}


def test_forgetting_leaves_links_pointing_at_it(sql: LinkDatabase) -> None:
    """Only the page's own row and outgoing links go. What other pages say
    about it is their data, not its."""
    sql.store("Bristol", ["Cheshire"])
    sql.store("Cheshire", ["Cheese"])

    sql.forget("Cheshire")

    assert sql.get_links(["Bristol"]) == {"Bristol": ["Cheshire"]}


def test_forgetting_a_title_that_is_not_there_changes_nothing(sql: LinkDatabase) -> None:
    assert sql.forget("Nowhere") == (0, 0)


def test_a_forgotten_missing_title_is_no_longer_recorded(sql: LinkDatabase) -> None:
    sql.mark_not_found(["Nowhere"])

    sql.forget("Nowhere")

    assert sql.page_type("Nowhere") is None
    assert sql.not_found_count() == 0
