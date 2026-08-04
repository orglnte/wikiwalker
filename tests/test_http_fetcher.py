"""Tests for reading pages over HTTP.

The distinction that matters most: a 404 means nobody wrote the article, and
anything else that goes wrong means we could not read it. Confusing the two
stores a live article as a dead end, which the search then treats as a valid
result — a wrong answer with no warning attached.

No network. `httpx.MockTransport` scripts every response.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from wikifetcher.http import HttpFetcher, PageUnavailable, RateLimited

SITE = "en.wikipedia.org"

PAGE = """<html><body>
<div id="mw-navigation"><a href="/wiki/Chrome">chrome</a></div>
<div id="mw-content-text"><div class="mw-parser-output">
<p><a href="/wiki/England">England</a> <a href="/wiki/Somerset">Somerset</a></p>
<p><a href="/wiki/Help:Contents">help</a></p>
</div></div>
</body></html>"""


def fetcher(handler, **kwargs) -> HttpFetcher:
    """An HttpFetcher wired to a scripted transport."""
    f = HttpFetcher(SITE, backoff_s=0.0, min_interval_s=0.0, **kwargs)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return f


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The three outcomes
# --------------------------------------------------------------------------

def test_a_page_yields_its_content_links_only() -> None:
    f = fetcher(lambda request: httpx.Response(200, text=PAGE))

    assert run(f.fetch("Bristol")) == ["England", "Somerset"]


def test_a_404_means_no_article() -> None:
    """The one status that is an answer rather than a failure."""
    f = fetcher(lambda request: httpx.Response(404))

    assert run(f.fetch("Zzz")) is None


def test_a_server_error_is_never_mistaken_for_a_missing_article() -> None:
    """If this returned None the caller would store a red link, and every walk
    through that page would silently return a longer path."""
    f = fetcher(lambda request: httpx.Response(500), retries=1)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))


def test_a_timeout_is_never_mistaken_for_a_missing_article() -> None:
    def timeout(request):
        raise httpx.ConnectTimeout("too slow")

    f = fetcher(timeout, retries=1)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))


# --------------------------------------------------------------------------
# Retrying
# --------------------------------------------------------------------------

def test_a_transient_error_is_retried_then_succeeds() -> None:
    attempts = []

    def flaky(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, text=PAGE)

    f = fetcher(flaky, retries=3)

    assert run(f.fetch("Bristol")) == ["England", "Somerset"]
    assert len(attempts) == 3


def test_retries_are_bounded() -> None:
    attempts = []

    def always_failing(request):
        attempts.append(request)
        return httpx.Response(502)

    f = fetcher(always_failing, retries=2)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))
    assert len(attempts) == 3  # the first try plus two retries


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

def test_a_429_is_not_retried() -> None:
    """429 is the site asking us to stop. Knocking again is not backing off."""
    attempts = []

    def limited(request):
        attempts.append(request)
        return httpx.Response(429, headers={"retry-after": "11"})

    f = fetcher(limited, retries=3)

    with pytest.raises(RateLimited):
        run(f.fetch("Bristol"))
    assert len(attempts) == 1


def test_one_429_stops_every_later_request() -> None:
    """Ten requests in flight each backing off independently is ten more
    requests. After the first refusal nothing else is sent."""
    attempts = []

    def limited(request):
        attempts.append(request)
        return httpx.Response(429)

    f = fetcher(limited)

    with pytest.raises(RateLimited):
        run(f.fetch("Bristol"))
    for title in ("Bath", "Wells", "Frome"):
        with pytest.raises(RateLimited):
            run(f.fetch(title))

    assert len(attempts) == 1


# --------------------------------------------------------------------------
# What counts as a link
# --------------------------------------------------------------------------

def test_chrome_and_non_article_links_are_left_out() -> None:
    """`Chrome` sits outside the content div, `Help:` is not an article."""
    f = fetcher(lambda request: httpx.Response(200, text=PAGE))

    links = run(f.fetch("Bristol"))

    assert "Chrome" not in links
    assert "Help:Contents" not in links


def test_the_requested_title_becomes_the_url() -> None:
    seen = []

    def record(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=PAGE)

    f = fetcher(record)
    run(f.fetch("Walton_Cardiff"))

    assert seen == ["https://en.wikipedia.org/wiki/Walton_Cardiff"]


def test_an_unexpected_status_is_a_failure_not_an_answer() -> None:
    f = fetcher(lambda request: httpx.Response(403))

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))


# --------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------

def test_requests_are_spaced_out_however_many_are_in_flight() -> None:
    """Concurrency is what we may run; pacing is what the site will tolerate.
    Ten at once with no interval is what earns a 429."""
    sent = []

    def record(request):
        sent.append(time.monotonic())
        return httpx.Response(200, text=PAGE)

    f = HttpFetcher(SITE, min_interval_s=0.05)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(record))

    async def five_at_once():
        await asyncio.gather(*(f.fetch(f"P{i}") for i in range(5)))

    started = time.monotonic()
    run(five_at_once())
    elapsed = time.monotonic() - started

    assert len(sent) == 5
    assert elapsed >= 4 * 0.05
    gaps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
    assert all(gap >= 0.04 for gap in gaps), gaps
