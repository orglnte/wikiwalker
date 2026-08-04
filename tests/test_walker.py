"""Tests for the breadth-first search.

Two properties matter and they pull in different directions:

    the path returned is the *shortest* one that exists, and
    the search says honestly whether it could see the whole graph.

The second is what red links are for. A link to a page that does not exist is a
genuine dead end and costs the answer nothing; a link to a page we simply never
fetched is a hole, and a search that walked into one cannot claim its path is
the shortest. Most of what follows checks those two stay apart.
"""

from __future__ import annotations

from link_store import LinkDatabase
from walker import Walker, endpoint_notes

# --------------------------------------------------------------------------
# Finding the shortest path
# --------------------------------------------------------------------------

def test_source_equal_to_target_is_a_zero_hop_path(graph: LinkDatabase) -> None:
    result = Walker(graph).find_path("Source", "Source")

    assert result.path == ["Source"]
    assert result.depth_reached == 0
    assert result.complete


def test_a_direct_link_is_one_hop(graph: LinkDatabase) -> None:
    result = Walker(graph).find_path("Source", "Middle")

    assert result.path == ["Source", "Middle"]
    assert result.depth_reached == 1


def test_the_shorter_of_two_routes_wins(graph: LinkDatabase) -> None:
    """Target is reachable in two hops via Middle and in three via Detour."""
    result = Walker(graph).find_path("Source", "Target")

    assert result.path == ["Source", "Middle", "Target"]
    assert result.depth_reached == 2
    assert result.complete


def test_titles_are_already_canonical_when_the_search_gets_them(db: LinkDatabase) -> None:
    """Spaces are normalised before the search, not inside it."""
    db.store("Walton_Cardiff", ["South_West_England"])
    db.store("South_West_England", [])

    result = Walker(db).find_path("Walton_Cardiff", "South_West_England")

    assert result.path == ["Walton_Cardiff", "South_West_England"]


def test_the_same_database_always_returns_the_same_path(db: LinkDatabase) -> None:
    """Two routes of equal length exist; the sorted frontier picks one and
    keeps picking it, so results are reproducible rather than dict-ordered."""
    db.store("Source", ["Beta", "Alpha"])
    db.store("Alpha", ["Target"])
    db.store("Beta", ["Target"])
    db.store("Target", [])

    paths = {tuple(Walker(db).find_path("Source", "Target").path) for _ in range(5)}

    assert paths == {("Source", "Alpha", "Target")}


# --------------------------------------------------------------------------
# Honesty about what the search could see
# --------------------------------------------------------------------------

def test_a_red_link_does_not_make_a_search_incomplete(graph: LinkDatabase) -> None:
    """`Nowhere` has no article behind it. Following it was never possible for
    anyone, so the search loses nothing by stopping there and must not report
    the result as doubtful on its account."""
    result = Walker(graph).find_path("Source", "Isolated")

    assert "Nowhere" not in result.unread


def test_a_page_we_never_fetched_does_make_a_search_incomplete(graph: LinkDatabase) -> None:
    """`Unfetched` is linked to but absent from the database. The path behind
    it is unknown, so no claim of shortest-ness survives it."""
    result = Walker(graph).find_path("Source", "Isolated")

    assert result.unread == {"Unfetched"}
    assert not result.complete


def test_an_unreachable_target_is_reported_completely_when_nothing_is_absent(
    db: LinkDatabase,
) -> None:
    """"No path exists" is a real answer, not a failure — but only when the
    whole reachable graph was visible."""
    db.store("Source", ["Neighbour"])
    db.store("Neighbour", [])
    db.store("Marooned", [])

    result = Walker(db).find_path("Source", "Marooned")

    assert result.path is None
    assert result.unread == set()
    assert result.complete


def test_an_unknown_source_yields_no_path_and_an_incomplete_search(db: LinkDatabase) -> None:
    db.store("Somewhere", [])

    result = Walker(db).find_path("Ghost", "Somewhere")

    assert result.path is None
    assert result.unread == {"Ghost"}
    assert not result.complete


def test_red_links_are_not_counted_as_pages_expanded(db: LinkDatabase) -> None:
    """Expanding is work done on a real page. A red link is neither fetched
    nor walked, so counting it would overstate what the search covered."""
    db.store("Source", ["Nowhere", "Real"])
    db.store("Real", [])
    db.store("Marooned", [])
    db.mark_red_links(["Nowhere"])

    result = Walker(db).find_path("Source", "Marooned")

    assert result.pages_expanded == 2  # Source and Real, not Nowhere


# --------------------------------------------------------------------------
# Endpoint status — the two ends are not symmetric
# --------------------------------------------------------------------------

def test_a_red_link_target_is_reachable(db: LinkDatabase) -> None:
    """The exercise reaches the target when a link equals it. A red link is a
    perfectly good link target, so the search must not refuse it."""
    db.store("Source", ["Nowhere"])
    db.mark_red_links(["Nowhere"])

    result = Walker(db).find_path("Source", "Nowhere")

    assert result.path == ["Source", "Nowhere"]
    assert result.target_status == "redlink"


def test_a_red_link_source_can_reach_nothing(db: LinkDatabase) -> None:
    """No article means no outgoing links, so no path can start here.

    Note this is not an incomplete search: nothing is absent from the database,
    the page simply does not exist.
    """
    db.store("Somewhere", ["Nowhere"])
    db.mark_red_links(["Nowhere"])

    result = Walker(db).find_path("Nowhere", "Somewhere")

    assert result.path is None
    assert result.source_status == "redlink"
    assert result.unread == set()
    assert result.complete


def test_an_impossible_search_is_settled_without_walking_the_graph(db: LinkDatabase) -> None:
    """Nothing links to an unknown title, so no sweep can find it. Deciding
    that after the search costs a full BFS — on simplewiki, 245k pages."""
    db.store("Source", ["Real"])
    db.store("Real", [])

    result = Walker(db).find_path("Source", "Phantom")

    assert result.path is None
    assert result.pages_expanded == 0


def test_endpoint_status_is_reported_for_ordinary_articles(graph: LinkDatabase) -> None:
    result = Walker(graph).find_path("Source", "Target")

    assert result.source_status == "ok"
    assert result.target_status == "ok"


def test_unknown_endpoints_have_no_status(db: LinkDatabase) -> None:
    db.store("Real", [])

    result = Walker(db).find_path("Ghost", "Phantom")

    assert result.source_status is None
    assert result.target_status is None


def test_notes_explain_a_red_link_target_without_calling_it_a_failure(
    db: LinkDatabase,
) -> None:
    db.store("Source", ["Nowhere"])
    db.mark_red_links(["Nowhere"])
    result = Walker(db).find_path("Source", "Nowhere")

    notes = endpoint_notes(result, "Source", "Nowhere", "simple.wikipedia.org")

    assert len(notes) == 1
    assert "red link" in notes[0]


def test_notes_name_which_endpoint_is_unknown(db: LinkDatabase) -> None:
    db.store("Real", [])
    result = Walker(db).find_path("Ghost", "Real")

    notes = endpoint_notes(result, "Ghost", "Real", "simple.wikipedia.org")

    assert len(notes) == 1
    assert "Ghost" in notes[0]


def test_articles_at_both_ends_need_no_explanation(graph: LinkDatabase) -> None:
    result = Walker(graph).find_path("Source", "Target")

    assert endpoint_notes(result, "Source", "Target", "simple.wikipedia.org") == []


# --------------------------------------------------------------------------
# Frontier discipline
# --------------------------------------------------------------------------

def test_a_page_linked_from_many_others_is_expanded_once(db: LinkDatabase) -> None:
    """Pages are marked visited when discovered, not when expanded.

    Marking at expansion instead would queue a hub once per inbound link, and
    the frontier would grow with the graph's density rather than its breadth.
    """
    db.store("Source", ["Alpha", "Beta"])
    db.store("Alpha", ["Hub"])
    db.store("Beta", ["Hub"])
    db.store("Hub", ["Target"])
    db.store("Target", [])

    result = Walker(db).find_path("Source", "Target")

    assert result.path == ["Source", "Alpha", "Hub", "Target"]
    assert result.pages_expanded == 4  # Source, Alpha, Beta, Hub — Hub only once


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def test_the_depth_limit_stops_the_search_short(db: LinkDatabase) -> None:
    db.store("Source", ["One"])
    db.store("One", ["Two"])
    db.store("Two", ["Target"])
    db.store("Target", [])

    result = Walker(db).find_path("Source", "Target", max_depth=2)

    assert result.path is None
    assert result.depth_reached == 2
    assert not result.complete, "ran out of budget, so nothing was proved"


def test_the_depth_limit_still_admits_a_path_exactly_at_the_limit(db: LinkDatabase) -> None:
    db.store("Source", ["One"])
    db.store("One", ["Target"])
    db.store("Target", [])

    result = Walker(db).find_path("Source", "Target", max_depth=2)

    assert result.path == ["Source", "One", "Target"]


def test_the_page_budget_stops_the_search_short(db: LinkDatabase) -> None:
    db.store("Source", ["One"])
    db.store("One", ["Target"])
    db.store("Target", [])

    result = Walker(db, batch_size=1).find_path("Source", "Target", max_walked=1)

    assert result.path is None
    assert not result.complete


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------

def test_a_frontier_larger_than_one_batch_is_handled(db: LinkDatabase) -> None:
    """The frontier is read from the database in batches, and a single BFS level
    is routinely wider than one batch. This crosses that seam."""
    fanout = [f"Leaf_{i:04d}" for i in range(1200)]
    db.store("Source", fanout)
    for leaf in fanout:
        db.store(leaf, [])
    db.store(fanout[-1], ["Target"])
    db.store("Target", [])

    result = Walker(db, batch_size=500).find_path("Source", "Target")

    assert result.path == ["Source", fanout[-1], "Target"]
    assert result.complete
