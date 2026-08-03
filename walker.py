#!/usr/bin/env python3
"""Find the shortest path between two Wikipedia articles, walking the link database.

Usage:
    python3 walker.py "Walton Cardiff" Bristol
    python3 walker.py Bristol London --max-depth 6
    python3 walker.py --test L0 L3_26

Reads the database `data_cli.py` builds. With `--test` it crawls a synthetic
wiki instead, filling the database as it goes.

Breadth-first because it visits every page at distance N before any at N+1, so
the target is found at its minimum distance. Depth-first finds *a* path, not the
shortest; Dijkstra degenerates to BFS on uniform edge costs.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field

import wikifetcher
from link_store import CachingLinkStore, LinkDatabase, LinkStore

log = logging.getLogger("walker")


@dataclass
class WalkResult:
    """Outcome of one walk.

    `complete` false means the walk stopped early (budget, or absent pages), so
    any path returned is valid but not provably the shortest.
    """

    path: list[str] | None
    depth_reached: int
    pages_expanded: int
    complete: bool
    elapsed_s: float = 0.0
    missing: set[str] = field(default_factory=set)

    # 'ok', 'redlink', or None (unknown). "No path" reads differently when the
    # source has no article behind it.
    source_status: str | None = None
    target_status: str | None = None

    @property
    def found(self) -> bool:
        return self.path is not None


class Walker:
    """Breadth-first search over a link store."""

    def __init__(self, store: LinkStore, *, batch_size: int = 5000) -> None:
        self._store = store
        # How many pages' link lists to hold at once. A BFS level can reach
        # hundreds of thousands of pages; all their links together is gigabytes.
        self._batch_size = batch_size

    def find_path(
        self,
        source: str,
        target: str,
        *,
        max_depth: int = 10,
        max_pages: int | None = None,
    ) -> WalkResult:
        """Find the shortest path of article links from `source` to `target`.

        Args:
            max_depth: give up beyond this many hops. A guardrail, not a workable
                depth — with a branching factor in the hundreds, depth 3 already
                covers much of the encyclopedia.
            max_pages: give up after expanding this many pages, so a hopeless
                pair fails visibly instead of looking like a hang.

        The ends are not symmetric: the source needs outgoing links, so it must
        be an article. The target only needs to be linked to, so a red link is
        reachable.
        """
        log.info("check endpoints: %s, %s", source, target)
        source_status = self._store.status(source)
        target_status = self._store.status(target)
        log.info("  %s is %s, %s is %s", source, source_status or "unknown",
                 target, target_status or "unknown")

        if source == target and source_status is not None:
            return WalkResult(
                [source], 0, 0, True,
                source_status=source_status, target_status=target_status,
            )

        # Checked before searching: a source with no article has nothing to
        # follow, and a title nothing links to can never be discovered. Proving
        # either by search costs a full sweep of the graph.
        if source_status != "ok" or target_status is None:
            unknown = {t for t, s in ((source, source_status), (target, target_status))
                       if s is None}
            return WalkResult(
                None, 0, 0, not unknown, missing=unknown,
                source_status=source_status, target_status=target_status,
            )

        result = self._search(source, target, max_depth=max_depth, max_pages=max_pages)
        result.source_status = source_status
        result.target_status = target_status
        return result

    def _search(
        self,
        source: str,
        target: str,
        *,
        max_depth: int,
        max_pages: int | None,
    ) -> WalkResult:
        """The breadth-first search itself. Titles are already canonical."""
        started = time.monotonic()

        # `parent` is three things at once, which is why BFS is so compact:
        #   1. the visited set         — membership test
        #   2. the shortest-path tree  — who discovered each page
        #   3. the reconstruction data — walk it backwards from the target
        # The source has no discoverer, hence None.
        parent: dict[str, str | None] = {source: None}

        # Link targets the database cannot explain: not articles, not red links.
        # Gaps in our data, so a search that hit one is not exhaustive. Red links
        # are excluded — a dead end costs the result nothing.
        missing: set[str] = set()

        # frontier - Standard graph-search term. The set of pages discovered but not yet expanded
        # boundary between explored and not yet explored
        frontier = [source]
        expanded = 0
        trace = log.isEnabledFor(logging.DEBUG)

        log.info("walk %s -> %s (max depth %d)", source, target, max_depth)

        for depth in range(max_depth):
            # All links at this frontier add to the next one
            next_frontier: list[str] = []
            log.info("depth %d: frontier %d page(s)", depth, len(frontier))

            # batched walk
            for start in range(0, len(frontier), self._batch_size):
                batch = frontier[start : start + self._batch_size]

                if max_pages is not None and expanded >= max_pages:
                    log.info("  stopping: page budget %d reached", max_pages)
                    return WalkResult(
                        None, depth, expanded, False,
                        time.monotonic() - started, missing,
                    )

                links = self._store.get_links(batch)

                # Split the rest into known red links and real gaps, one query
                # per batch rather than one per page.
                unexplained = [page for page in batch if page not in links]
                if unexplained:
                    dead_ends = self._store.red_links(unexplained)
                    gaps = [page for page in unexplained if page not in dead_ends]
                    missing.update(gaps)
                    log.info(
                        "  batch of %d: %d article(s), %d dead end(s), %d gap(s)",
                        len(batch), len(links), len(dead_ends), len(gaps),
                    )
                else:
                    log.info("  batch of %d: all articles", len(batch))

                for page in batch:
                    outgoing_links = links.get(page)
                    if outgoing_links is None:
                        continue

                    expanded += 1
                    discovered = 0

                    for link in outgoing_links:
                        if link in parent:
                            # Already discovered at this depth or a shallower one
                            continue

                        # Visited at discovery, not expansion — otherwise a hub
                        # is queued once per inbound link.
                        parent[link] = page
                        discovered += 1

                        # NOTE its found!
                        if link == target:
                            log.info("  %s links to %s — found", page, target)
                            return WalkResult(
                                # NOTE reverts the path so it's source -> target
                                _reconstruct(parent, target),
                                depth + 1,
                                expanded,
                                not missing,
                                time.monotonic() - started,
                                missing,
                            )

                        next_frontier.append(link)

                    if trace:
                        log.debug(
                            "    expand %s: %d link(s), %d new",
                            page, len(outgoing_links), discovered,
                        )

            if not next_frontier:
                # Nothing new to explore: the target is unreachable from the source.
                log.info("nothing new at depth %d: %s is unreachable", depth, target)
                return WalkResult(
                    None, depth, expanded, not missing,
                    time.monotonic() - started, missing,
                )

            # Equally short paths usually exist; sorting makes the choice
            # between them reproducible instead of dict-ordered.
            frontier = sorted(next_frontier)

        # Ran out of depth budget with the target still unseen.
        return WalkResult(
            None, max_depth, expanded, False, time.monotonic() - started, missing
        )

# TODO why
def _reconstruct(parent: dict[str, str | None], target: str) -> list[str]:
    """Walk the parent chain from `target` back to the source, then reverse it."""
    path = [target]
    page = parent[target]
    while page is not None:
        path.append(page)
        page = parent[page]
    path.reverse()
    return path


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def print_path(path: list[str], site: str) -> None:
    """Print the path as a step / name / link table."""
    name_width = max(len("Name"), *(len(wikifetcher.to_name(t)) for t in path))

    print(f"{'Step':<5} {'Name':<{name_width}}  Link")
    print(f"{'-' * 5} {'-' * name_width}  {'-' * 40}")
    for step, title in enumerate(path, start=1):
        print(f"{step:<5} {wikifetcher.to_name(title):<{name_width}}  {wikifetcher.to_url(title, site)}")


def endpoint_notes(result: WalkResult, source: str, target: str, site: str) -> list[str]:
    """Explain an endpoint when its status changes how to read the result."""
    notes: list[str] = []

    if result.source_status is None:
        notes.append(
            f"{wikifetcher.to_name(source)} is not in the database — check the spelling, "
            f"or it may not exist on {site}."
        )
    elif result.source_status == "redlink":
        notes.append(
            f"{wikifetcher.to_name(source)} has no article on {site}: other pages link to it, "
            f"but there is nothing to link out from, so no path can start here."
        )

    if result.target_status is None:
        notes.append(
            f"{wikifetcher.to_name(target)} is not in the database — check the spelling, "
            f"or it may not exist on {site}. Nothing links to it, so nothing can reach it."
        )
    elif result.target_status == "redlink":
        # Not an error: a red link is still a valid link target.
        notes.append(
            f"{wikifetcher.to_name(target)} is a red link on {site}: pages link to it, "
            f"but no article exists. It can be reached, never departed from."
        )

    return notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="starting article title")
    parser.add_argument("target", help="article title to reach")
    parser.add_argument("--db", default=None,
                        help="link database (default: test.db with --test, else simplewiki.db)")
    parser.add_argument("--max-depth", type=int, default=10,
                        help="maximum path length in links (default: 10)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="stop after expanding this many pages")
    parser.add_argument("--test", action="store_true",
                        help="crawl the synthetic wiki instead of reading a loaded one")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log every page expanded, fetched and stored")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress the step-by-step log in test mode")
    args = parser.parse_args()

    # Test mode exists to be watched, so it narrates by default. -v adds the
    # per-page detail; -q silences it.
    if args.verbose:
        level = logging.DEBUG
    elif args.test and not args.quiet:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(level=level, format="%(name)-19s %(message)s", stream=sys.stderr)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # Typed titles become canonical titles here. Past this point nothing knows the
    # graph is a wiki.
    source, target = wikifetcher.canonical(args.source), wikifetcher.canonical(args.target)

    db_path = args.db or ("test.db" if args.test else "simplewiki.db")

    with LinkDatabase(db_path) as db:
        if args.test:
            fetcher = wikifetcher.LocalFetcher()
            store: LinkStore = CachingLinkStore(db, fetcher)
            site = fetcher.site
            db.set_meta("site", site)
        else:
            if db.page_count() == 0:
                sys.exit(
                    f"{db_path} is empty — populate it first:\n"
                    f"    python3 data_cli.py simplewiki --db {db_path}"
                )
            store = db
            # The store records which wiki it holds, so links render against
            # the right host rather than an assumed one.
            site = db.get_meta("site", wikifetcher.DEFAULT_SITE)

        result = Walker(store).find_path(
            source, target,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
        )

    notes = endpoint_notes(result, source, target, site)

    if result.found:
        print_path(result.path, site)
    elif result.source_status != "ok" or result.target_status is None:
        print(f"No path from {wikifetcher.to_name(source)} to {wikifetcher.to_name(target)}.")
    else:
        print(f"No path found within {result.depth_reached} links.")

    for note in notes:
        print(f"\nNOTE: {note}")

    # Diagnostics go to stderr; flush first so the two streams stay in order.
    sys.stdout.flush()
    print(
        f"\n{result.pages_expanded:,} pages expanded in {result.elapsed_s:.2f}s"
        f" | depth {result.depth_reached}",
        file=sys.stderr,
    )

    if not result.complete:
        # A partial search must not pass for an exhaustive one. Name which kind
        # of partial it was — absent data and an exhausted budget are different
        # problems with different fixes.
        cause = (
            f"{len(result.missing):,} page{'' if len(result.missing) == 1 else 's'}"
            f" absent from the database"
            if result.missing
            else "stopped at the depth or page budget"
        )
        caveat = (
            "the path found may not be the shortest"
            if result.found
            else "a path may exist beyond what was searched"
        )
        print(f"WARNING: search was not exhaustive ({cause}) — {caveat}.", file=sys.stderr)

    sys.exit(0 if result.found else 1)


if __name__ == "__main__":
    main()
