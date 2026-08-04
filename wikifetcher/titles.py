"""What a wiki title is, and how it maps to and from a URL.

The canonical form is the one MediaWiki puts in a URL: spaces as underscores,
percent-escapes decoded. Everything that produces a title must agree on it —
`Café_wall_illusion` stored twice under two spellings would be searched twice.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from settings import DEFAULT_SITE

# Namespaces that are not articles. Matched against the prefix before the first
# colon — not a blunt "contains a colon" test, since real articles have one
# ("Star Trek: The Next Generation").
NON_ARTICLE_NAMESPACES = frozenset(
    ns.lower()
    for ns in (
        "Media", "Special", "Talk", "User", "User talk",
        "Wikipedia", "Wikipedia talk", "WP", "WT",
        "File", "File talk", "Image", "Image talk",
        "MediaWiki", "MediaWiki talk",
        "Template", "Template talk",
        "Help", "Help talk",
        "Category", "Category talk",
        "Portal", "Portal talk",
        "Draft", "Draft talk",
        "TimedText", "TimedText talk",
        "Module", "Module talk",
        "Book", "Education Program", "Gadget", "Gadget definition",
        "Topic",
    )
)

# A wiki serves two renderings and they spell links differently. The legacy PHP
# parser emits path-relative hrefs; Parsoid emits absolute URLs, sometimes
# `./Title`. Knowing only one form returns nothing on pages served by the other.
#
#   /wiki/Bristol                          legacy
#   ./Bristol                              parsoid, relative
#   https://en.wikipedia.org/wiki/Bristol  parsoid, absolute
#   //en.wikipedia.org/wiki/Bristol        protocol-relative
#
# A trailing `#section` is part of the same article, so it is stripped. A query
# string is not: red links carry one (`?action=edit&redlink=1`) and there is no
# article behind them, so an href holding one matches nothing.
_PATH_HREF = re.compile(r"^(?:\./|/wiki/)(?P<title>[^#?]+)(?:#.*)?$")
_ABSOLUTE_HREF = re.compile(r"^(?:https?:)?//(?P<host>[^/]+)/wiki/(?P<title>[^#?]+)(?:#.*)?$")


def canonical(title: str) -> str:
    """Normalise a title typed by a human into the form links use."""
    return title.strip().replace(" ", "_")


def from_href(href: str, site: str = DEFAULT_SITE) -> str | None:
    """The article title an href points at, or None if it isn't one.

    Rejects, in order: anything not pointing at an article on this wiki
    (external links, red links, and interlanguage links — which differ from
    ordinary ones only by host); then anything in a non-article namespace.
    """
    match = _PATH_HREF.match(href)
    if match is None:
        match = _ABSOLUTE_HREF.match(href)
        if match is None or match.group("host") != site:
            return None

    # `/wiki/Bristol` and `/wiki/Bristol#History` collapse to one title.
    title = unquote(match.group("title"))

    prefix, sep, _ = title.partition(":")
    if sep and prefix.replace("_", " ").lower() in NON_ARTICLE_NAMESPACES:
        return None

    return title


def to_url(title: str, site: str = DEFAULT_SITE) -> str:
    """The canonical URL for a title on `site`.

    The host is a parameter because the same title names different articles on
    different wikis; assuming one silently points at the wrong article.
    """
    return f"https://{site}/wiki/{quote(title, safe=':_()')}"


def to_name(title: str) -> str:
    """Human-readable form: underscores in a title are really spaces."""
    return title.replace("_", " ")
