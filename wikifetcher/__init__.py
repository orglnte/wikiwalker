"""Everything that knows this is a wiki.

    titles.py       what a title is, and how it maps to and from a URL
    html_links.py   finding article links in page HTML
    base.py         the Fetcher interface
    local.py        serves the sample wiki, no network
    sample_wiki.py  the corpus local.py serves

Nothing outside this package needs to know that nodes are wiki articles. A
fetcher takes titles and returns the titles they link to; the search and the
store treat both as opaque names in a graph.
"""

from .base import MAX_CONCURRENCY, USER_AGENT, Fetcher, Page, PageFetchFailed
from .html_links import extract_links
from .http import HttpFetcher, PageUnavailable
from .local import LocalFetcher
from .titles import DEFAULT_SITE, canonical, to_name, to_url

__all__ = [
    "DEFAULT_SITE",
    "MAX_CONCURRENCY",
    "USER_AGENT",
    "Fetcher",
    "HttpFetcher",
    "LocalFetcher",
    "Page",
    "PageFetchFailed",
    "PageUnavailable",
    "canonical",
    "extract_links",
    "to_name",
    "to_url",
]
