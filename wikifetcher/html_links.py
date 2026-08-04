"""Find the article links in a wiki page's HTML.

Deliberately not a DOM parser. One thing is needed from a ~300KB page — the
`<a href>` targets — and building an element tree costs 10-100x more CPU than a
streaming pass. At ten concurrent fetches that is the line between parsing being
free and parsing being the bottleneck.

`html.parser.HTMLParser` is a stdlib SAX-style scanner: it calls back per tag
and no tree is ever built. What an href *means* is `titles.from_href`'s job.
"""

from __future__ import annotations

from html.parser import HTMLParser

from .titles import DEFAULT_SITE, from_href

# The article body, including the navbox templates at the bottom. Outside it is
# site chrome — search box, sidebar, footer — whose links are real but would
# connect every page to every other through the site furniture.
_CONTENT_DIV_ID = "mw-content-text"


class ArticleLinkParser(HTMLParser):
    """Reads what a page says about itself: which article it is, and what it
    links to.

    Usage:
        parser = ArticleLinkParser()
        parser.feed(html)
        parser.canonical  # -> "United_Kingdom"
        parser.links      # -> ["South_West_England", "Gloucestershire", ...]
    """

    def __init__(self, *, site: str = DEFAULT_SITE, content_only: bool = True) -> None:
        # convert_charrefs is on by default, so hrefs arrive unescaped.
        super().__init__()

        # Distinguishes a link within this wiki from one to the same article in
        # another language: in Parsoid output both are absolute URLs and only
        # the host tells them apart.
        self._site = site
        self._content_only = content_only

        # Depth counter rather than a flag: the content div holds many nested
        # divs, and only the matching close tag ends the content region.
        self._div_depth = 0
        self._in_content = not content_only

        # dict rather than set: keeps first-seen order, so repeated runs over
        # one page produce the same ordering.
        self._links: dict[str, None] = {}

        # The article this page really is. A wiki serves a redirect's target
        # under the redirect's own URL, so the address asked for does not say.
        self.canonical: str | None = None

    @property
    def links(self) -> list[str]:
        return list(self._links)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "link":
            values = dict(attrs)
            if values.get("rel") == "canonical" and values.get("href"):
                self.canonical = from_href(values["href"], self._site)
            return

        if tag == "div":
            if self._in_content:
                self._div_depth += 1
            elif self._content_only:
                for name, value in attrs:
                    if name == "id" and value == _CONTENT_DIV_ID:
                        self._in_content = True
                        self._div_depth = 1
                        break
            return

        if tag != "a" or not self._in_content:
            return

        for name, value in attrs:
            if name == "href" and value:
                title = from_href(value, self._site)
                if title is not None:
                    self._links.setdefault(title, None)
                break

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._in_content and self._content_only:
            self._div_depth -= 1
            if self._div_depth == 0:
                self._in_content = False


def read_page(
    html: str, *, site: str = DEFAULT_SITE, content_only: bool = True
) -> tuple[str | None, list[str]]:
    """The article `html` is, and the titles it links to in first-seen order.

    The first is None when the page does not name itself, which the sample wiki
    and any hand-written fixture will not.
    """
    parser = ArticleLinkParser(site=site, content_only=content_only)
    parser.feed(html)
    parser.close()
    return parser.canonical, parser.links


def extract_links(
    html: str, *, site: str = DEFAULT_SITE, content_only: bool = True
) -> list[str]:
    """Return the article titles linked from `html`, in first-seen order."""
    return read_page(html, site=site, content_only=content_only)[1]


if __name__ == "__main__":
    import sys
    import time

    from .titles import to_name

    # Measure parse cost on a saved page, no network:
    #     curl -sL https://en.wikipedia.org/wiki/Bristol > tmp/bristol.html
    #     python3 -m wikifetcher.html_links tmp/bristol.html
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as page:
            raw = page.read()
    else:
        raw = sys.stdin.read()

    start = time.perf_counter()
    links = extract_links(raw)
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"{len(links)} article links, parsed {len(raw) / 1024:.0f}KB in {elapsed_ms:.1f}ms")
    for title in links[:20]:
        print(f"  {to_name(title)}")
