#!/usr/bin/env python3
"""Find the shortest path between two Wikipedia articles, walking the link database.

Usage:
    python3 walker.py read-only Bristol Cheese
    python3 walker.py test L0 L3_26
    python3 walker.py crawl Bristol England


Breadth-first visits every page at distance N before any at N+1, so the target
is found at its minimum distance.
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
import sysconfig
import time
from dataclasses import dataclass, field

import logctx
import wikifetcher
from link_store import UNKNOWN, LinkStore
from settings import (
    CRAWL_FETCH_BUDGET,
    MAX_CONCURRENCY,
    MAX_CONSECUTIVE_FAILURES,
    MIN_REQUEST_INTERVAL_S,
    WALK_BATCH_SIZE,
)

log = logging.getLogger("walker")


@dataclass(frozen=True)
class Mode:
    """One functional scenario: which store, which fetcher, what budget."""

    store: str
    fetcher: type[wikifetcher.Fetcher] | None
    fetch_budget: int | None
    summary: str


MODES = {
    "read-only": Mode(
        "simplewiki", None, fetch_budget=None,
        summary="read-only, never fetches (for testing)",
    ),
    "test": Mode(
        "test", wikifetcher.LocalFetcher, fetch_budget=None,
        summary="synthetic 41-page wiki, no network (for testing)",
    ),
    "crawl": Mode(
        "wikipedia-us", wikifetcher.HttpFetcher,
        fetch_budget=CRAWL_FETCH_BUDGET,
        summary=f"fetches what is missing over HTTP, at most {CRAWL_FETCH_BUDGET} pages",
    ),
}




STATE_MEANING = {
    "article": "an article, so it has links to follow",
    "redlink": "no article behind the title, so nothing leads out of it",
    "unknown": "the store has never looked at it",
}


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
    unread: set[str] = field(default_factory=set)

    # What the store knew about the target when the walk started.
    target_state: str | None = None

    # 'ok', 'redlink', or None (unknown). "No path" reads differently when the
    # source has no article behind it.
    source_status: str | None = None
    target_status: str | None = None

    # Why an endpoint could not be read, when something went wrong reading it.
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.path is not None


class Walker:
    """Breadth-first search over a link store."""

    def __init__(self, store: LinkStore, *, batch_size: int = WALK_BATCH_SIZE) -> None:
        self._store = store
        self._batch_size = batch_size

    def find_path(
        self,
        source: str,
        target: str,
        *,
        max_depth: int = 10,
        max_walked: int | None = None,
    ) -> WalkResult:
        """Find the shortest path of article links from `source` to `target`.

        Args:
            max_depth: give up beyond this many hops. A guardrail, not a workable
                depth — with a branching factor in the hundreds, depth 3 already
                covers much of the encyclopedia.
            max_walked: give up after expanding this many pages, so a hopeless
                pair fails visibly instead of looking like a hang.

        The ends are not symmetric: the source needs outgoing links, so it must
        be an article. The target only needs to be linked to, so a red link is
        reachable.
        """
        log.info("check endpoints: %s, %s (source must be an article)", source, target)
        source_status = self._store.status(source)
        target_status = self._store.status(target)
        store_failures = getattr(self._store, "failures", {})
        endpoint_failures = {
            t: store_failures[t] for t in (source, target) if t in store_failures
        }
        states = {}
        for title in (source, target):
            states[title] = self._store.state(title)
            # A read that failed is not the same as one never attempted, and
            # `unknown` covers both.
            why = endpoint_failures.get(title) or STATE_MEANING[states[title]]
            log.info("    %s is %s (%s)", title, states[title], why)

        if source == target and source_status is not None:
            return WalkResult(
                [source], 0, 0, True,
                source_status=source_status, target_status=target_status,
                target_state=states[target],
            )

        # Checked before searching: a source with no article has nothing to
        # follow, and a title nothing links to can never be discovered. Proving
        # either by search costs a full sweep of the graph.
        if source_status != "ok" or target_status is None:
            unknown = {t for t, s in ((source, source_status), (target, target_status))
                       if s is None}
            return WalkResult(
                None, 0, 0, not unknown, unread=unknown,
                source_status=source_status, target_status=target_status,
                target_state=states[target], failures=endpoint_failures,
            )

        result = self._search(source, target, max_depth=max_depth, max_walked=max_walked)
        result.source_status = source_status
        result.target_status = target_status
        result.target_state = states[target]
        result.failures = endpoint_failures
        return result

    def _search(
        self,
        source: str,
        target: str,
        *,
        max_depth: int,
        max_walked: int | None,
    ) -> WalkResult:
        """The breadth-first search itself. Titles are already canonical."""
        started = time.monotonic()

        # `parent` is three things at once, which is why BFS is so compact:
        #   1. the visited set         — membership test
        #   2. the shortest-path tree  — who discovered each page
        #   3. the reconstruction data — walk it backwards from the target
        # The source has no discoverer, hence None.
        parent: dict[str, str | None] = {source: None}

        # Pages the walk needed and could not read: a gap in a dump, a failed
        # fetch in a crawl. Red links are not here — those are real dead ends.
        unread: set[str] = set()

        # frontier - Standard graph-search term. The set of pages discovered but not yet expanded
        # boundary between explored and not yet explored
        frontier = [source]
        expanded = 0
        trace = log.isEnabledFor(logging.DEBUG)

        log.info("walk %s -> %s (max depth %d)", source, target, max_depth)

        for depth in range(max_depth):
            # All links at this frontier add to the next one
            next_frontier: list[str] = []
            batch_number = 0
            logctx.depth.set(depth)

            # batched walk
            for start in range(0, len(frontier), self._batch_size):
                batch = frontier[start : start + self._batch_size]
                batch_number += 1
                log.info(
                    "  depth %d batch %d links %d (max links %d)",
                    depth, batch_number, len(batch), self._batch_size,
                )
                links = self._store.get_links(batch)
                in_batch = {"expanded": 0, "dead": 0, "unread": 0}

                for page in batch:
                    # Check if we are over the max pages budget
                    if max_walked is not None and expanded >= max_walked:
                        log.info("    stopping: page budget %d reached", max_walked)
                        return WalkResult(
                            None, depth, expanded, False,
                            time.monotonic() - started, unread,
                        )

                    # NOTE this call blocks until the page is fetched (if not in db)
                    outgoing_links = links.get(page, UNKNOWN)

                    if outgoing_links is UNKNOWN:
                        # Could not be read: a gap in a dump, a failed fetch in
                        # a crawl. Either way the walk is not exhaustive.
                        # NOTE to be defined if retry or not, if sleep/wait or not
                        # if you re-crawl manually, it will retry only the failed fetches.
                        unread.add(page)
                        in_batch["unread"] += 1
                        continue

                    # TODO is this red links?
                    if outgoing_links is None:
                        # No article behind the title. A real dead end, and it
                        # costs the answer nothing.
                        in_batch["dead"] += 1
                        continue

                    expanded += 1
                    in_batch["expanded"] += 1
                    discovered = 0

                    for link in outgoing_links:
                        if link in parent:
                            # Already discovered at this depth or a shallower one
                            continue

                        # TODO fix this comment
                        # Visited at discovery, not expansion — otherwise a hub
                        # is queued once per inbound link.
                        parent[link] = page
                        discovered += 1

                        # NOTE its found!
                        if link == target:
                            log.info("    %s links to %s — found", page, target)
                            return WalkResult(
                                # NOTE extracts the path from source to target
                                _reconstruct(parent, target),
                                depth + 1,
                                expanded,
                                not unread,
                                time.monotonic() - started,
                                unread,
                            )

                        next_frontier.append(link)

                    if trace:
                        log.debug(
                            "      expand %s: %d link(s), %d new",
                            page, len(outgoing_links), discovered,
                        )

                if in_batch["dead"] or in_batch["unread"]:
                    log.info(
                        "    %d expanded, %d dead end(s), %d unread",
                        in_batch["expanded"], in_batch["dead"], in_batch["unread"],
                    )

            if not next_frontier:
                # Nothing new to explore: the target is unreachable from the source.
                log.info("  nothing new at depth %d: %s is unreachable", depth, target)
                return WalkResult(
                    None, depth, expanded, not unread,
                    time.monotonic() - started, unread,
                )

            # Equally short paths usually exist; sorting makes the choice
            # between them reproducible instead of dict-ordered.
            frontier = sorted(next_frontier)

        # Ran out of depth budget with the target still unseen.
        return WalkResult(
            None, max_depth, expanded, False, time.monotonic() - started, unread
        )

def _reconstruct(parent: dict[str, str | None], target: str) -> list[str]:
    """Walk the parent chain from `target` back to the source, then reverse it.

    `parent` holds every page discovered so far.

        parent = {
            "L0": None,
            "L1_00": "L0",    "L1_01": "L0",    "L1_02": "L0",
            "L2_00": "L1_00", "L2_01": "L1_00", "L2_02": "L1_00",
            "L3_26": "L1_00",
        }

    `_reconstruct(parent, "L3_26")` then returns:

        ["L0", "L1_00", "L3_26"]

    Only the source maps to None, so None stops the loop.
    """
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
        print(
            f"{step:<5} {wikifetcher.to_name(title):<{name_width}}  "
            f"{wikifetcher.to_url(title, site)}"
        )


def endpoint_notes(result: WalkResult, source: str, target: str, site: str) -> list[str]:
    """Explain an endpoint when its status changes how to read the result."""
    notes: list[str] = []

    if result.source_status is None:
        why = result.failures.get(source)
        notes.append(
            f"{wikifetcher.to_name(source)} could not be read"
            + (f" ({why})." if why else ".")
        )
    elif result.source_status == "redlink":
        notes.append(
            f"{wikifetcher.to_name(source)} has no article on {site}, so there is nothing "
            f"to link out from and no path can start here."
        )

    if result.target_status is None:
        why = result.failures.get(target)
        notes.append(
            f"{wikifetcher.to_name(target)} could not be read"
            + (f" ({why})." if why else ".")
            + " It may well have no article behind it, which is a target the"
            " walk accepts — pages can link to a title nobody has written."
        )
    elif result.target_status == "redlink":
        # Not an error: a title with no article behind it is still a link target.
        notes.append(
            f"{wikifetcher.to_name(target)} has no article on {site}. Nothing leads out of "
            f"it, but a page linking to it would still reach it."
        )

    return notes


def warn_if_gil_reenabled() -> None:
    """Say so when a free-threaded run silently lost its parallelism.

    Importing a C extension that has not declared free-threading support turns
    the GIL back on for the whole process, with no error. Fetching and parsing
    then serialise, and nothing else would show it.
    """
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        return
    if not sys._is_gil_enabled():
        return

    print(
        "WARNING: the GIL was re-enabled at runtime — an imported C extension "
        "has not declared free-threading support, so fetching and parsing ran "
        "serialised. Re-run with -W error::RuntimeWarning to find which.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    """One subcommand per scenario, so each carries only its own options."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("source", help="starting article title")
    common.add_argument("target", help="article title to reach")
    common.add_argument("--db", default=None,
                        help="link store to use (default: picked by the scenario)")
    common.add_argument("--max-depth", type=int, default=10,
                        help="maximum path length in links (default: 10)")
    common.add_argument("--max-walked-pages", type=int, default=None,
                        help="stop after walking this many pages")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="log every page expanded, fetched and stored")

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=" ",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    scenarios = parser.add_subparsers(dest="mode", required=True, metavar="SCENARIO")

    for name, mode in MODES.items():
        sub = scenarios.add_parser(
            name, parents=[common], help=mode.summary,
            description=f"{name}: {mode.summary}\n\nUses the {mode.store} store.",
            epilog=" ", formatter_class=argparse.RawTextHelpFormatter,
        )
        if name != "test":
            sub.add_argument("--batch-size", type=int, default=WALK_BATCH_SIZE,
                             help="pages read or fetched per round "
                                  f"(default: {WALK_BATCH_SIZE})")
        if mode.fetcher is wikifetcher.HttpFetcher:
            sub.add_argument("--max-fetched-pages", type=int, default=mode.fetch_budget,
                             help="stop retrieving after this many pages "
                                  f"(default: {mode.fetch_budget})")
            sub.add_argument("--walk-anyway", action="store_true",
                             help="keep walking after the site stops answering, "
                                  "using only what the store already holds")
            sub.add_argument("--max-concurrency", type=int, default=MAX_CONCURRENCY,
                             help=f"requests in flight at once (default: {MAX_CONCURRENCY})")
            sub.add_argument("--max-rps", type=float,
                             default=1.0 / MIN_REQUEST_INTERVAL_S,
                             help="requests per second, 0 for no limit "
                                  f"(default: {1.0 / MIN_REQUEST_INTERVAL_S:g})")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    mode = MODES[args.mode]

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(name)-19s %(message)s", stream=sys.stderr)
    for noisy in ("asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Typed titles become canonical titles here. Past this point nothing knows the
    # graph is a wiki.
    source, target = wikifetcher.canonical(args.source), wikifetcher.canonical(args.target)

    store_name = args.db or mode.store
    max_fetched = getattr(args, "max_fetched_pages", None)

    fetcher = mode.fetcher
    if fetcher is wikifetcher.HttpFetcher:
        interval = 1.0 / args.max_rps if args.max_rps > 0 else 0.0
        fetcher = functools.partial(
            wikifetcher.HttpFetcher,
            min_interval_s=interval,
            failure_limit=None if args.walk_anyway else MAX_CONSECUTIVE_FAILURES,
        )

    concurrency = getattr(args, "max_concurrency", MAX_CONCURRENCY)

    with LinkStore(
        store_name, fetcher, concurrency=concurrency, max_fetched=max_fetched
    ) as store:
        if fetcher is None and store.page_count() == 0:
            sys.exit(
                f"{store_name} is empty — populate it first:\n"
                f"    python3 data_cli.py simplewiki --db {store_name}"
            )

        # The store records which wiki it holds, so links render against the
        # right host rather than an assumed one.
        site = store.site or wikifetcher.DEFAULT_SITE

        batch_size = getattr(args, "batch_size", WALK_BATCH_SIZE)
        result = Walker(store, batch_size=batch_size).find_path(
            source, target,
            max_depth=args.max_depth,
            max_walked=args.max_walked_pages,
        )
        budget_spent = store.budget_spent
        fetched = store.fetched

    notes = endpoint_notes(result, source, target, site)

    if result.found:
        print_path(result.path, site)
    elif result.source_status != "ok" or result.target_status is None:
        print(f"\nNo path from {wikifetcher.to_name(source)} to {wikifetcher.to_name(target)}.")
    else:
        print(f"\nNo path found within {result.depth_reached} links.")

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
            f"{len(result.unread):,} page{'' if len(result.unread) == 1 else 's'}"
            f" absent from the database"
            if result.unread
            else "stopped at the depth or page budget"
        )
        caveat = (
            "the path found may not be the shortest"
            if result.found
            else "a path may exist beyond what was searched"
        )
        print(f"WARNING: search was not exhaustive ({cause}) — {caveat}.", file=sys.stderr)

    if budget_spent:
        # Says which limit ended the crawl. Without it an exhausted budget looks
        # like a graph that ran out, since both leave the frontier empty.
        print(
            f"NOTE: the fetch budget of {fetched:,} page(s) was exhausted."
            " Raise --max-fetched-pages to search further; what was fetched is kept,"
            " so the next run carries on from it.",
            file=sys.stderr,
        )

    warn_if_gil_reenabled()
    sys.exit(0 if result.found else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Every page the crawl read is already written, so the next run resumes
        # from it. 130 is what a shell expects from a process killed by SIGINT.
        print("\nInterrupted. Pages already fetched are kept.", file=sys.stderr)
        sys.exit(130)
