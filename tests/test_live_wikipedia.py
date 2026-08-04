"""Checks `extract_links` against live en.wikipedia, on pages nobody chose.

Every other test feeds the parser markup written for it. This one takes random
articles and compares what the parser finds against MediaWiki's own `pagelinks`
table, read through the API — the same table the dump import reads, so the two
sources of a page's links are held to agreeing.

Opt-in, since it needs the network:

    pytest -m live
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass

import httpx
import pytest

from settings import USER_AGENT
from wikifetcher.html_links import extract_links
from wikifetcher.http import HttpFetcher
from wikifetcher.titles import NON_ARTICLE_NAMESPACES

pytestmark = pytest.mark.live

SITE = "en.wikipedia.org"
API = f"https://{SITE}/w/api.php"

# Enough articles that the budget below has a denominator worth dividing by:
# one coordinate link lost from a stub is 5% of that stub alone, and at ten
# articles a sample without many section links stays under budget by luck.
SAMPLE = 30

# How many of MediaWiki's own links a read may fail to return, as a share of
# them. Measured at 0.21% over 30 random articles, all of it one link
# (`Geographic_coordinate_system`) on the articles carrying coordinates.
#
# A share rather than a list of titles: what a page renders outside its content
# div is a property of the templates it uses, and naming them here would make
# the test track Wikipedia's furniture instead of the parser.
LOST_LINK_BUDGET = 0.0075


@pytest.fixture(scope="module")
def client():
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as session:
        yield session


def query(client: httpx.Client, **params) -> dict:
    return client.get(
        API, params={"action": "query", "format": "json", "formatversion": "2", **params}
    ).json()


@pytest.fixture(scope="module")
def articles(client: httpx.Client) -> list[str]:
    """Random articles, so no page in this file was picked to make it pass."""
    data = query(client, list="random", rnnamespace="0", rnlimit=str(SAMPLE))
    return [page["title"].replace(" ", "_") for page in data["query"]["random"]]


def linked_from(client: httpx.Client, title: str) -> set[str]:
    """The articles MediaWiki records this page as linking to.

    Red links are left out: `pagelinks` records them, but a page with no
    article behind it is rendered as an edit link rather than as `/wiki/`, so
    the parser is right not to return one.
    """
    links: set[str] = set()
    params = {
        "titles": title.replace("_", " "),
        "prop": "links", "plnamespace": "0", "pllimit": "max",
    }
    while True:
        data = query(client, **params)
        for page in data["query"]["pages"]:
            for link in page.get("links", []):
                links.add(link["title"].replace(" ", "_"))
        if "continue" not in data:
            break
        params.update(data["continue"])

    return links - missing(client, links)


def missing(client: httpx.Client, titles: set[str]) -> set[str]:
    """Which of these titles have no article behind them."""
    absent: set[str] = set()
    ordered = sorted(titles)
    for start in range(0, len(ordered), 50):
        batch = ordered[start : start + 50]
        data = query(client, titles="|".join(t.replace("_", " ") for t in batch))
        for page in data["query"]["pages"]:
            if page.get("missing"):
                absent.add(page["title"].replace(" ", "_"))
    return absent


@dataclass(frozen=True)
class Sampled:
    """One article, read both ways: MediaWiki's answer and ours."""

    title: str
    recorded: set[str]      # what pagelinks says, red links removed
    body: set[str]          # what the parser finds inside the content div
    whole: set[str]         # what it finds across the page


@pytest.fixture(scope="module")
def sample(client: httpx.Client, articles: list[str]) -> list[Sampled]:
    """Read every article once. Each test then asks a different question of it."""
    read = []
    for title in articles:
        html = client.get(f"https://{SITE}/wiki/{title}", follow_redirects=True).text
        read.append(Sampled(
            title=title,
            recorded=linked_from(client, title),
            body=set(extract_links(html, site=SITE)),
            whole=set(extract_links(html, site=SITE, content_only=False)),
        ))
    return read


def assert_within_budget(
    found: dict[str, set[str]], sample: list[Sampled], what: str, budget: float
) -> None:
    """Fail unless `found` accounts for all but `budget` of the recorded links.

    Budgeted across the sample rather than per page: what goes missing is
    roughly one link per article that has any, so on a short article that one
    link is a large share of a small number and says nothing about the parser.
    """
    lost = total = 0
    examples: set[str] = set()

    for page in sample:
        missing_here = page.recorded - found[page.title]
        lost += len(missing_here)
        total += len(page.recorded)
        examples |= missing_here

    assert lost <= budget * total, (
        f"{what} lost {lost}/{total} recorded links ({lost / max(total, 1):.2%}, "
        f"budget {budget:.2%}): {sorted(examples)[:10]}"
    )


def test_report_what_the_parser_sees_of_the_whole_page(sample: list[Sampled]) -> None:
    """Reports, never fails.

    Read across the whole page rather than the content div, so the only reason
    a recorded link can be absent is the parser not seeing an `<a href>` that
    the HTML carries. That number should be zero, but it is a measurement of
    live pages rather than a promise about them, so it is reported and left to
    a person to judge.
    """
    lost = total = 0
    examples: set[str] = set()

    for page in sample:
        unseen = page.recorded - page.whole
        lost += len(unseen)
        total += len(page.recorded)
        examples |= unseen

    if lost:
        warnings.warn(
            f"parser saw {total - lost}/{total} recorded links "
            f"({1 - lost / max(total, 1):.2%}), missed {sorted(examples)[:10]}",
            stacklevel=1,
        )


def test_the_fetcher_returns_what_the_mediawiki_api_records(sample: list[Sampled]) -> None:
    """The whole path — HTTP, status handling, parse — against MediaWiki's own
    answer: what a walk actually stores, against what the wiki says the page
    links to.

    The budget is the content div's. A page's links are read from inside it,
    so whatever a page renders outside — the coordinates in the header, the
    furniture around the article — is missing here by choice, not by defect.
    """

    async def fetch_all() -> dict[str, list[str] | None]:
        # One loop for the lot: the client outlives a single fetch.
        fetcher = HttpFetcher(SITE)
        try:
            return {page.title: await fetcher.fetch(page.title) for page in sample}
        finally:
            await fetcher.aclose()

    fetched = asyncio.run(fetch_all())
    for page in sample:
        assert fetched[page.title] is not None, f"{page.title}: a random article must exist"
        assert set(fetched[page.title]) == page.body, f"{page.title}: fetcher != parser"

    assert_within_budget(
        {title: set(links or ()) for title, links in fetched.items()},
        sample, "the fetcher", LOST_LINK_BUDGET,
    )


def test_nothing_outside_the_article_namespace_is_returned(sample: list[Sampled]) -> None:
    """A `Category:` or `Help:` title stored as an article would be walked as
    one, and those pages link to hundreds of unrelated articles."""
    for page in sample:
        for linked in page.body:
            prefix, sep, _ = linked.partition(":")
            assert not sep or prefix.replace("_", " ").lower() not in NON_ARTICLE_NAMESPACES
