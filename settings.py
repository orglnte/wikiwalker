"""Everything you would change to run this differently.

Operational knobs only. Facts about wikis — which namespaces are articles, what
an href looks like, the database schema — stay next to the code that knows them.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------

# One database per dataset, so building one cannot destroy another. A name not
# listed here is taken as a path, which is how tests ask for ":memory:".
DB_FILE = {
    "test": "test.db",
    "simplewiki": "simplewiki.db",
    "wikipedia-us": "wikipedia-us.db",
}

# Which wiki each dataset holds, so a fetcher knows where to read from.
SITE = {
    "simplewiki": "simple.wikipedia.org",
    "wikipedia-us": "en.wikipedia.org",
}

DEFAULT_SITE = "en.wikipedia.org"

DUMPS_URL = "https://dumps.wikimedia.org"

# --------------------------------------------------------------------------
# How hard we are willing to hit a wiki
#
# Two separate limits. Concurrency is how many requests may be outstanding;
# the interval is how often one may leave. Wikimedia publishes no rate for
# article HTML — robots.txt sets a crawl delay for one named bot and otherwise
# asks that bots be "low-speed" — so the interval below is a choice, not a
# quoted figure, and `--max-rps` exists to lower it.
# --------------------------------------------------------------------------

USER_AGENT = "wikiwalker/0.1 (https://github.com/orglnte/wikiwalker)"

MAX_CONCURRENCY = 10
MIN_REQUEST_INTERVAL_S = 0.1

HTTP_TIMEOUT_S = 30.0
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 1.0

# HTTP redirects only — protocol and host normalisation. A wiki's own redirects
# are not these: it serves the target's page under the alias, with no 3xx.
MAX_REDIRECTS = 3

# Retried, with backoff. 429 is deliberately absent: that is the server asking
# us to stop, and retrying it is not backing off, it is knocking again.
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

# One page failing is that page's problem; this many in a row is the site's.
MAX_CONSECUTIVE_FAILURES = 10

# A 429 carries Retry-After, which is a resume time rather than a refusal, so
# it is honoured. Being told to wait this many times means we are unwelcome.
MAX_RATE_LIMIT_PAUSES = 3

# --------------------------------------------------------------------------
# Searching
# --------------------------------------------------------------------------

# A crawl with no budget walks the encyclopedia one request at a time. At the
# interval above that is hours, so a crawl gets a ceiling unless one is given.
CRAWL_FETCH_BUDGET = 500

# How many pages' links to hold at once. A BFS level can reach hundreds of
# thousands of pages; all their links together is gigabytes.
WALK_BATCH_SIZE = 5000

# How long to wait for one page before giving up on it.
FETCH_TIMEOUT_S = 5.0

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

# A red link becomes an article only when somebody writes one, so re-checking
# one every run buys nothing.
RED_LINK_TTL_S = 24 * 60 * 60

# SQLite's host-parameter cap per statement is 999 on older builds. Every
# IN (...) query batches below it.
SQLITE_PARAM_BATCH = 900
