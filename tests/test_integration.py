"""Checks against a real Wikipedia database, skipped when `simplewiki.db` is absent.

Covers what a hand-built fixture cannot: that the dump load produced the shape
the rest of the code assumes. Assertions are on invariants, not on counts, which
move with every new dump.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from link_store import LinkStore
from walker import Walker

WIKI_DB = Path(__file__).resolve().parent.parent / "simplewiki.db"

pytestmark = pytest.mark.skipif(
    not WIKI_DB.exists(),
    reason=f"{WIKI_DB.name} not built — run `python3 data_cli.py simplewiki`",
)


@pytest.fixture(scope="module")
def wiki() -> LinkStore:
    with LinkStore(str(WIKI_DB)) as database:
        yield database


def test_the_database_records_which_wiki_it_holds(wiki: LinkStore) -> None:
    """Without this the tool renders links against an assumed host and can
    point at a different article of the same name on another wiki."""
    assert wiki.get_meta("site") in {"simple.wikipedia.org", "en.wikipedia.org"}


def test_the_load_produced_articles_links_and_missing_titles(wiki: LinkStore) -> None:
    assert wiki.page_count() > 100_000
    assert wiki.link_count() > 1_000_000
    assert wiki.not_found_count() > 0, (
        "no red links recorded — every link target resolved to an article, "
        "which does not happen on a real wiki and points at the ETL"
    )


def test_every_link_target_is_either_an_article_a_redirect_or_missing(wiki: LinkStore) -> None:
    """A target that is neither is a hole, and a search crossing one can no
    longer claim its answer is the shortest."""
    sample = random.Random(0).sample(sorted(wiki.get_links(_some_titles(wiki))), 50)
    targets = {
        link for links in wiki.get_links(sample).values() if links for link in links
    }

    known = wiki.get_links(targets)
    unexplained = sorted(t for t in targets if t not in known)

    assert unexplained == [], f"{len(unexplained)} unexplained targets, e.g. {unexplained[:5]}"


def test_a_known_search_returns_a_short_verified_path(wiki: LinkStore) -> None:
    """The exact depth moves with each dump, so the assertion is on the path
    being real: every consecutive pair must be an edge that exists."""
    result = Walker(wiki).find_path("April", "Nigeria")

    assert result.path is not None
    assert result.path[0] == "April" and result.path[-1] == "Nigeria"
    assert result.depth_reached <= 3
    assert result.complete, f"search hit {len(result.unread)} unread pages"

    links = wiki.get_links(result.path[:-1])
    pairs = zip(result.path, result.path[1:], strict=False)
    for step, (src, dst) in enumerate(pairs, start=1):
        assert dst in links[src], f"step {step}: {src} does not link to {dst}"


def _some_titles(wiki: LinkStore) -> list[str]:
    """A handful of real article titles to start the sampling from."""
    return [
        title
        for (title,) in wiki._db._conn.execute(
            "SELECT title FROM pages WHERE status = 'article' LIMIT 200"
        )
    ]
