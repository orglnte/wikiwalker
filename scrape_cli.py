#!/usr/bin/env python3
"""Fetch one Wikipedia page and print its article links.

Manual probe for checking what the parser actually extracts from a real page,
and how long parsing takes. Stdlib only.

Usage:
    python3 scrape_cli.py https://en.wikipedia.org/wiki/Walton_Cardiff
    python3 scrape_cli.py Bristol
"""

import sys
import time
import urllib.request
from urllib.parse import urlsplit

import wikifetcher

USER_AGENT = wikifetcher.USER_AGENT


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <wikipedia url or article title>")

    arg = sys.argv[1]
    url = arg if arg.startswith("http") else f"https://en.wikipedia.org/wiki/{arg}"

    # The parser needs the wiki's own host so it can tell an ordinary link from
    # an interlanguage link to another wiki — in Parsoid output both are
    # absolute URLs and only the host distinguishes them.
    site = urlsplit(url).netloc

    start = time.perf_counter()
    html = fetch(url)
    fetched = time.perf_counter()
    links = wikifetcher.extract_links(html, site=site)
    parsed = time.perf_counter()

    for title in links:
        print(wikifetcher.to_name(title))

    print(
        f"\n{len(links)} links | {len(html) / 1024:.0f}KB | "
        f"fetch {(fetched - start) * 1000:.0f}ms | parse {(parsed - fetched) * 1000:.1f}ms",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
