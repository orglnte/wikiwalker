#!/usr/bin/env python3
"""Which titles the wiki links to most.

Usage:
    python3 topk_cli.py --wiki simplewiki
    python3 topk_cli.py --wiki simplewiki -n 20

Counts rows in `links` by destination, as the pages write them: a redirect keeps
its own tally rather than being folded into the article it names. No index on
`dst` — the search only ever walks forwards — so this is a full scan, about 7s
over simplewiki's 18M links.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from link_store import DB_FILE
from wikifetcher import to_name

# No join to `pages` for the type: it costs four times the scan, and only the
# handful of rows surviving the LIMIT need it.
MOST_LINKED = """
SELECT dst AS target, COUNT(*) AS links
FROM links
GROUP BY target
ORDER BY links DESC
LIMIT ?
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--wiki", default="simplewiki", choices=sorted(DB_FILE))
    parser.add_argument("--db", help="database file, overriding --wiki")
    parser.add_argument("-n", type=int, default=5, help="how many to report (default 5)")
    args = parser.parse_args()

    path = Path(args.db or DB_FILE[args.wiki])
    if not path.exists():
        sys.exit(f"{path} does not exist — build it with data_cli.py")

    # Said before the scan, which takes seconds and shows nothing.
    print(f"Top {args.n} most linked titles in {path}\n")
    sys.stdout.flush()

    started = time.monotonic()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        rows = conn.execute(MOST_LINKED, (args.n,)).fetchall()

        kind = {
            title: page_type
            for title, page_type in conn.execute(
                "SELECT title, page_type FROM pages WHERE title IN "
                f"({','.join('?' * len(rows))})",
                [target for target, _ in rows],
            )
        }

    width = max((len(to_name(t)) for t, _ in rows), default=5)
    print(f"{'Links':>10}  {'Type':<9} Title")
    print(f"{'-' * 10}  {'-' * 9} {'-' * width}")
    for target, links in rows:
        print(f"{links:>10,}  {kind.get(target, 'unknown'):<9} {to_name(target)}")

    print(f"\n{len(rows)} of {path} in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
