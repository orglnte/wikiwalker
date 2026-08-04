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

from wikifetcher.html_links import read_page
from wikifetcher.http import (
    HttpFetcher,
    PageUnavailable,
    RateLimited,
    SiteUnreachable,
)

SITE = "en.wikipedia.org"

PAGE = """<html><body>
<div id="mw-navigation"><a href="/wiki/Chrome">chrome</a></div>
<div id="mw-content-text"><div class="mw-parser-output">
<p><a href="/wiki/England">England</a> <a href="/wiki/Somerset">Somerset</a></p>
<p><a href="/wiki/Help:Contents">help</a></p>
</div></div>
</body></html>"""


REDIRECTED = """<html><head>
<link rel="canonical" href="https://en.wikipedia.org/wiki/United_Kingdom"/>
</head><body>
<div id="mw-content-text"><div class="mw-parser-output">
<p><a href="/wiki/England">England</a> <a href="/wiki/Wales">Wales</a></p>
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

    assert run(f.fetch("Bristol")).links == ["England", "Somerset"]


def test_a_404_means_no_article() -> None:
    """The one status that is an answer rather than a failure."""
    f = fetcher(lambda request: httpx.Response(404))

    assert run(f.fetch("Zzz")).links is None


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
# Redirects
# --------------------------------------------------------------------------

def test_a_redirect_is_recognised_from_the_page_not_the_response() -> None:
    """A wiki serves the target's HTML under the redirect's own URL, with no
    3xx and nothing in the response history. Only the page says which article
    it is, so reading the response alone records the alias as an article."""
    seen = []

    def serve(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=REDIRECTED)

    page = run(fetcher(serve).fetch("UK"))

    assert seen == ["https://en.wikipedia.org/wiki/UK"]      # no redirect followed
    assert page.title == "United_Kingdom"
    assert page.aliases == ["UK"]
    assert page.links == ["England", "Wales"]


def test_a_page_that_is_itself_claims_no_alias() -> None:
    def serve(request):
        return httpx.Response(200, text=REDIRECTED)

    page = run(fetcher(serve).fetch("United_Kingdom"))

    assert page.title == "United_Kingdom"
    assert page.aliases == []


def test_a_page_without_a_canonical_link_keeps_the_title_asked_for() -> None:
    """Nothing guarantees the tag is there, and a missing one is not a
    redirect — it just means the page did not say."""
    page = run(fetcher(lambda request: httpx.Response(200, text=PAGE)).fetch("Bristol"))

    assert page.title == "Bristol"
    assert page.aliases == []


def test_the_canonical_title_comes_out_of_the_head() -> None:
    canonical, links = read_page(REDIRECTED, site=SITE)

    assert canonical == "United_Kingdom"
    assert links == ["England", "Wales"]


def test_a_canonical_link_to_another_wiki_is_not_this_pages_title() -> None:
    """Same rule as any other href: the host is what tells them apart."""
    elsewhere = REDIRECTED.replace("en.wikipedia.org", "de.wikipedia.org")

    assert read_page(elsewhere, site=SITE)[0] is None


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

    assert run(f.fetch("Bristol")).links == ["England", "Somerset"]
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

def test_a_429_pauses_and_then_retries() -> None:
    """Retry-After is a resume time, not a refusal, so it is honoured."""
    codes = iter([429, 200])

    def scripted(request):
        code = next(codes)
        return httpx.Response(code, text=PAGE if code == 200 else "")

    f = fetcher(scripted, retries=3)

    assert run(f.fetch("Bristol")).links == ["England", "Somerset"]


def test_a_429_sets_a_pause_rather_than_failing_the_page() -> None:
    """Retry-After becomes a resume time held on the fetcher, not this call."""
    def limited(request):
        return httpx.Response(429, headers={"retry-after": "5"})

    f = fetcher(limited, retries=0, pause_limit=5)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))

    assert f._resume_at > time.monotonic() + 4


def test_being_told_to_wait_too_often_stops_the_fetcher() -> None:
    attempts = []

    def limited(request):
        attempts.append(request)
        return httpx.Response(429)

    f = fetcher(limited, retries=10, pause_limit=2)

    with pytest.raises(RateLimited):
        run(f.fetch("Bristol"))
    assert len(attempts) == 3           # two pauses accepted, the third refused

    before = len(attempts)
    with pytest.raises(RateLimited):
        run(f.fetch("Bath"))
    assert len(attempts) == before      # nothing sent after it has stopped


# --------------------------------------------------------------------------
# What counts as a link
# --------------------------------------------------------------------------

def test_chrome_and_non_article_links_are_left_out() -> None:
    """`Chrome` sits outside the content div, `Help:` is not an article."""
    f = fetcher(lambda request: httpx.Response(200, text=PAGE))

    links = run(f.fetch("Bristol")).links

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


# --------------------------------------------------------------------------
# Giving up on the site
# --------------------------------------------------------------------------

def test_one_page_failing_does_not_stop_the_others() -> None:
    """A broken page is that page's problem. The next title still goes out."""
    def only_bristol_fails(request):
        if "Bristol" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, text=PAGE)

    f = fetcher(only_bristol_fails, retries=0, failure_limit=3)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))
    assert run(f.fetch("Bath")).links == ["England", "Somerset"]


def test_enough_failures_in_a_row_stop_the_fetcher() -> None:
    attempts = []

    def always_failing(request):
        attempts.append(request)
        return httpx.Response(500)

    f = fetcher(always_failing, retries=0, failure_limit=3)

    for _ in range(2):
        with pytest.raises(PageUnavailable):
            run(f.fetch("X"))
    with pytest.raises(SiteUnreachable):
        run(f.fetch("X"))

    # nothing is sent once it has stopped
    before = len(attempts)
    with pytest.raises(RateLimited):
        run(f.fetch("Y"))
    assert len(attempts) == before


def test_a_success_clears_the_failure_run() -> None:
    """Two failures, a success, two more failures must not trip a limit of 3."""
    responses = iter([500, 500, 200, 500, 500])

    def scripted(request):
        code = next(responses)
        return httpx.Response(code, text=PAGE if code == 200 else "")

    f = fetcher(scripted, retries=0, failure_limit=3)

    for _ in range(2):
        with pytest.raises(PageUnavailable):
            run(f.fetch("X"))
    assert run(f.fetch("X")).links == ["England", "Somerset"]
    for _ in range(2):
        with pytest.raises(PageUnavailable):
            run(f.fetch("X"))


def test_the_limit_can_be_lifted() -> None:
    """What --walk-anyway does: never stop on the site's account."""
    def always_failing(request):
        return httpx.Response(500)

    f = fetcher(always_failing, retries=0, failure_limit=None)

    for _ in range(20):
        with pytest.raises(PageUnavailable):
            run(f.fetch("X"))
    assert not f._stopped


# --------------------------------------------------------------------------
# Which statuses are retried
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried(status: int) -> None:
    attempts = []

    def failing(request):
        attempts.append(request)
        return httpx.Response(status)

    f = fetcher(failing, retries=2)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))
    assert len(attempts) == 3


@pytest.mark.parametrize("status", [400, 403, 451])
def test_client_errors_are_not_retried(status: int) -> None:
    """Asking again cannot change the answer."""
    attempts = []

    def refusing(request):
        attempts.append(request)
        return httpx.Response(status)

    f = fetcher(refusing, retries=3)

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))
    assert len(attempts) == 1


def test_backoff_grows_between_attempts() -> None:
    sent = []

    def failing(request):
        sent.append(time.monotonic())
        return httpx.Response(503)

    f = HttpFetcher(SITE, backoff_s=0.02, min_interval_s=0.0, retries=2)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(failing))

    with pytest.raises(PageUnavailable):
        run(f.fetch("Bristol"))

    gaps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
    assert len(gaps) == 2
    assert gaps[1] > gaps[0]        # 0.02s then 0.04s


def test_a_429_holds_back_requests_that_have_not_gone_out_yet() -> None:
    """The gate is on the fetcher, so requests still queued wait behind it.

    Requests already on the wire cannot be recalled — this covers the ones
    that have not left.
    """
    sent = []
    refused_at = []

    def scripted(request):
        sent.append(time.monotonic())
        if not refused_at:
            refused_at.append(time.monotonic())
            return httpx.Response(429)
        return httpx.Response(200, text=PAGE)

    f = HttpFetcher(SITE, min_interval_s=0.02, backoff_s=0.1, retries=3, pause_limit=5)
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(scripted))

    async def several():
        await asyncio.gather(*(f.fetch(f"P{i}") for i in range(5)))

    run(several())

    # everything sent after the refusal waited for the pause the fetcher set
    after = [t for t in sent if t > refused_at[0]]
    assert after, "no requests followed the 429"
    assert min(after) - refused_at[0] >= 0.15, [t - refused_at[0] for t in after]
