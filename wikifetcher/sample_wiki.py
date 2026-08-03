"""A synthetic wiki: 41 articles in a 1 -> 3 -> 9 -> 27 tree, plus an orphan.

One definition, two consumers: `data_cli.py test` loads it in bulk and
`LocalFetcher` serves it page by page. Both must produce the same database, so
neither may know anything the other does not.

Small enough that a whole search fits in a log you can read.
"""

from __future__ import annotations

LEVELS = 3
FANOUT = 3
ROOT = "L0"

# Not a real host: nothing here exists on any wiki.
SITE = "test.invalid"

# Linked to by an article, but no article exists.
RED_LINKS = frozenset({"Red_Link"})

# An article nothing links to. Searching for it from the root reaches every
# page without ever finding it, which is what a full crawl of this wiki is.
ORPHAN = "Orphan"


def _build() -> dict[str, list[str]]:
    pages: dict[str, list[str]] = {}
    frontier = [ROOT]

    for depth in range(1, LEVELS + 1):
        children = [f"L{depth}_{i:02d}" for i in range(len(frontier) * FANOUT)]
        for n, parent in enumerate(frontier):
            pages[parent] = children[n * FANOUT : (n + 1) * FANOUT]
        frontier = children

    for leaf in frontier:
        pages[leaf] = []

    # A shortcut across the tree: the last leaf is at depth 2 this way and
    # depth 3 down the branches, so BFS must report 2.
    pages["L1_00"].append(frontier[-1])

    # One article pointing at a title with no article behind it.
    pages[frontier[0]].append(next(iter(RED_LINKS)))

    pages[ORPHAN] = []

    return pages


GRAPH = _build()


# The three decoy links must all be dropped: two by the content-div filter
# (ordinary article links, just outside the body) and one by the namespace
# filter (inside the body, but not an article).
_PAGE = """<!DOCTYPE html>
<html><head><title>{name}</title></head><body>
<div id="mw-navigation"><a href="/wiki/{root}">{root}</a></div>
<div id="mw-content-text"><div class="mw-parser-output">
<p>{links}</p>
<p><a href="/wiki/Help:Contents">Help</a></p>
</div></div>
<div id="footer"><a href="/wiki/{root}">{root}</a></div>
</body></html>
"""


def render(title: str) -> str | None:
    """The article's HTML, or None if no article exists at that title."""
    links = GRAPH.get(title)
    if links is None:
        return None

    anchors = "".join(
        f'<a href="/wiki/{link}">{link.replace("_", " ")}</a> ' for link in links
    )
    return _PAGE.format(name=title.replace("_", " "), links=anchors, root=ROOT)
