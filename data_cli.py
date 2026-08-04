#!/usr/bin/env python3
"""Create the link database the search runs against.

Four modes, one database each so building one cannot destroy another:

    test        41 synthetic pages, 1 -> 3 -> 9 -> 27      -> test.db
    simplewiki  Simple English Wikipedia, ~283k articles   -> simplewiki.db
    wikipedia-us  Full English Wikipedia, gigabytes        -> wikipedia-us.db
    empty       schema and site metadata only, to be crawled into

Usage:
    python3 data_cli.py test
    python3 data_cli.py simplewiki
    python3 data_cli.py wikipedia-us
    python3 data_cli.py empty --wiki simplewiki

The dump modes parse nothing in Python. MediaWiki dumps are MySQL INSERT
statements whose string literals contain commas, parens and escaped quotes, so a
correct parser scans character by character — about an hour per 50GB in Python.
Two native tools do it instead:

    gzcat dump.sql.gz | wikiparse-rs --table X --format csv | sqlite3 ".import"

Everything after the import is SQL. Requires `cargo install wikiparse-rs`.
"""

from __future__ import annotations

import argparse
import gzip
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import wikifetcher
from link_store import DB_FILE, LinkDatabase, PageStatus
from settings import DUMPS_URL as BASE_URL
from wikifetcher import sample_wiki

USER_AGENT = wikifetcher.USER_AGENT

# `page` maps ids to titles and flags redirects; `pagelinks` holds the edges.
# Since ~2024 pagelinks stores an id into `linktarget` rather than the
# destination title, so that third file is needed to resolve targets.
DUMP_TABLES = ("page", "linktarget", "pagelinks", "redirect")

MAIN_NAMESPACE = "0"  # compared as text; see `stage`

# English wikis only. The namespace filter in `wikifetcher/titles.py` lists English
# prefixes, so another language's dump would admit `Catégorie:` and `Portale:`
# pages as articles — and those are hubs linking to thousands of pages, which
# yields short paths that are not article paths.
# name -> (Wikimedia dump prefix, site). The dump prefix is Wikimedia's; the
# name is ours, and is what `walker.py --db` takes.
SUPPORTED_WIKIS = {
    "wikipedia-us": ("enwiki", "en.wikipedia.org"),
    "simplewiki": ("simplewiki", "simple.wikipedia.org"),
}

MODES = ("test", "simplewiki", "wikipedia-us", "empty", "forget")


def forget(db_path: str, title: str | None) -> None:
    """Drop one article, so a later walk has to retrieve it again.

    Makes the read-through visible: the same walk answers from the store
    before, and from the network after.
    """
    if not title:
        sys.exit("forget needs --title")
    if not Path(db_path).exists():
        sys.exit(f"{db_path} does not exist")

    with LinkDatabase(db_path) as db:
        pages, links = db.forget(title)

    if not pages:
        sys.exit(f"{title} is not in {db_path}")
    print(f"Forgot {title} from {db_path}: dropped {links} outgoing link(s).")
    print("It is now unknown, so the next walk that reaches it will fetch it.")


def default_db(mode: str, wiki: str) -> str:
    name = "test" if mode == "test" else (wiki if mode == "empty" else mode)
    return DB_FILE[name]


# --------------------------------------------------------------------------
# test mode
# --------------------------------------------------------------------------

def populate_test(db: LinkDatabase) -> None:
    """Load the sample wiki in bulk.

    Must leave exactly what crawling it through `LocalFetcher` leaves, or the
    two ways of building the test database diverge.
    """
    graph = sample_wiki.GRAPH
    db.bulk_write(
        [*graph.items(), *((title, None) for title in sorted(sample_wiki.RED_LINKS))]
    )
    db.set_meta("site", sample_wiki.SITE)
    db.set_meta("wiki", "test")

    edges = sum(len(v) for v in graph.values())
    print(f"{len(graph)} articles, {len(sample_wiki.RED_LINKS)} red links, {edges} edges")
    print(f"  root      {sample_wiki.ROOT}")
    print("  shortcut  L1_00 -> L3_26   (depth 2, not 3)")
    print("  red link  L3_00 -> Red_Link")


# --------------------------------------------------------------------------
# dump modes
# --------------------------------------------------------------------------

def find_parser() -> str:
    found = shutil.which("wikiparse-rs") or shutil.which(
        "wikiparse-rs", path=str(Path.home() / ".cargo" / "bin")
    )
    if not found:
        sys.exit("wikiparse-rs not found. Install it with:\n    cargo install wikiparse-rs")
    return found


def download(wiki: str, table: str, into: Path) -> Path:
    """Fetch one dump file, resuming or skipping if already present."""
    name = f"{wiki}-latest-{table}.sql.gz"
    dest = into / name

    if dest.exists():
        print(f"  {name}: present ({dest.stat().st_size / 1e6:.0f} MB)")
        return dest

    url = f"{BASE_URL}/{wiki}/latest/{name}"
    print(f"  {name}: downloading")

    # Renamed only on success, so an interrupted transfer is never mistaken for
    # a complete file next run. `-C -` resumes.
    partial = dest.with_suffix(".part")
    subprocess.run(
        ["curl", "-fL", "-C", "-", "-A", USER_AGENT, "--progress-bar", "-o", str(partial), url],
        check=True,
    )
    partial.rename(dest)
    return dest


_COLUMN = re.compile(r"^\s+`(?P<name>\w+)`\s")


def dump_columns(dump: Path) -> list[str]:
    """Read column names from the dump's own CREATE TABLE.

    The dump is the authority on its own layout. wikiparse-rs v0.1.2 emits a
    pagelinks CSV header whose column order does not match the rows it writes;
    importing with that header swaps two columns, and every later join then
    matches namespaces against target ids and returns nothing, without error.
    """
    columns: list[str] = []
    inside = False

    with gzip.open(dump, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not inside:
                inside = line.startswith("CREATE TABLE")
                continue
            match = _COLUMN.match(line)
            if match:
                columns.append(match.group("name"))
            elif line.lstrip().startswith((")", "PRIMARY KEY", "KEY", "UNIQUE")):
                break

    if not columns:
        sys.exit(f"could not read column names from {dump.name}")
    return columns


def stage(parser: str, dump: Path, table: str, db_path: str) -> None:
    """Load one dump file into a staging table.

    Columns are staged as TEXT. SQLite's CSV importer carries no type
    information, and every join below compares ids the dump writes in one
    consistent plain-integer form, so text comparison agrees with numeric.
    """
    started = time.monotonic()
    print(f"Loading {dump.name}")

    # Table created up front from the schema's columns so `.import` fills it
    # rather than inventing one from the CSV header, which `--skip 1` discards.
    columns = dump_columns(dump)
    conn = sqlite3.connect(db_path)
    conn.execute(f"DROP TABLE IF EXISTS t_{table}")
    conn.execute(f"CREATE TABLE t_{table} ({', '.join(f'{c} TEXT' for c in columns)})")
    conn.commit()
    conn.close()

    # Streamed end to end; nothing is held whole in memory.
    reader = subprocess.Popen(["gzcat", str(dump)], stdout=subprocess.PIPE)
    parsed = subprocess.Popen(
        [parser, "--table", table, "--format", "csv", "--input", "-"],
        stdin=reader.stdout,
        stdout=subprocess.PIPE,
    )
    reader.stdout.close()  # let gzcat see EPIPE if the parser dies

    importer = subprocess.Popen(
        ["sqlite3", db_path, f".import --csv --skip 1 /dev/stdin t_{table}"],
        stdin=parsed.stdout,
    )
    parsed.stdout.close()

    importer.wait()
    parsed.wait()
    reader.wait()

    if importer.returncode or parsed.returncode or reader.returncode:
        sys.exit(f"failed loading {dump.name}")

    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"SELECT COUNT(*) FROM t_{table}").fetchone()[0]
    conn.close()
    print(f"  {rows:,} rows in {time.monotonic() - started:.0f}s")


def build(db_path: str, *, keep_staging: bool = False) -> None:
    """Turn the staged MediaWiki tables into the `pages` / `links` schema."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA cache_size=-500000;
        """
    )

    print("Indexing staged tables...")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS ix_page_id    ON t_page(page_id);
        CREATE INDEX IF NOT EXISTS ix_page_title ON t_page(page_title);
        CREATE INDEX IF NOT EXISTS ix_lt_id      ON t_linktarget(lt_id);
        CREATE INDEX IF NOT EXISTS ix_pl_from    ON t_pagelinks(pl_from);
        CREATE INDEX IF NOT EXISTS ix_rd_from    ON t_redirect(rd_from);
        """
    )

    # An edge pointing at a redirect must point at what the redirect leads to,
    # or the same article appears under several names and the visited set breaks.
    print("Resolving redirects...")
    conn.executescript(
        f"""
        DROP TABLE IF EXISTS t_target;
        CREATE TABLE t_target AS
        SELECT lt.lt_id AS target_id, lt.lt_title AS title
        FROM t_linktarget lt
        WHERE lt.lt_namespace = '{MAIN_NAMESPACE}';

        CREATE INDEX ix_target ON t_target(target_id);
        """
    )

    print("Building links...")
    conn.commit()
    with LinkDatabase(db_path) as store:
        store.build_from_staging(MAIN_NAMESPACE)

    counted = "SELECT COUNT(*) FROM pages WHERE status = ?"
    pages = conn.execute(counted, (PageStatus.ARTICLE,)).fetchone()[0]
    missing = conn.execute(counted, (PageStatus.NOTFOUND,)).fetchone()[0]
    redirects = conn.execute(counted, (PageStatus.REDIRECT,)).fetchone()[0]
    links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]

    if keep_staging:
        print("Keeping staging tables (--keep-staging)")
    else:
        print("Dropping staging tables...")
        for table in (*DUMP_TABLES, "target"):
            conn.execute(f"DROP TABLE IF EXISTS t_{table}")
        conn.commit()
        conn.execute("VACUUM")

    conn.close()
    print(
        f"\n{pages:,} articles, {redirects:,} redirects, "
        f"{missing:,} not found, {links:,} links"
    )


def load_dump(wiki: str, work: Path, dump_dir: Path, *, keep_staging: bool) -> None:
    parser = find_parser()
    dump_dir.mkdir(parents=True, exist_ok=True)

    dump, site = SUPPORTED_WIKIS[wiki]

    with LinkDatabase(str(work)) as db:
        db.set_meta("site", site)
        db.set_meta("wiki", wiki)

    print(f"Fetching {dump} dumps into {dump_dir}/")
    dumps = {table: download(dump, table, dump_dir) for table in DUMP_TABLES}

    for table in DUMP_TABLES:
        stage(parser, dumps[table], table, str(work))

    build(str(work), keep_staging=keep_staging)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main() -> None:
    argparser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    argparser.add_argument("mode", choices=MODES, help="which dataset to build")
    argparser.add_argument("--title", default=None,
                           help="article to forget, so the next walk retrieves it again")
    argparser.add_argument("--db", default=None,
                           help="output database (default depends on mode)")
    argparser.add_argument("--wiki", default="wikipedia-us", choices=sorted(SUPPORTED_WIKIS),
                           help="which site an empty database is for")
    argparser.add_argument("--dumps", default="tmp/dumps", help="where downloads are kept")
    argparser.add_argument("--keep-staging", action="store_true",
                           help="do not drop the raw MediaWiki tables afterwards")
    argparser.add_argument("--force", action="store_true",
                           help="overwrite an existing database")
    args = argparser.parse_args()

    if args.mode == "forget":
        forget(args.db or default_db("simplewiki", args.wiki), args.title)
        return

    target = Path(args.db or default_db(args.mode, args.wiki))
    if target.exists() and not args.force:
        sys.exit(
            f"{target} already exists ({target.stat().st_size / 1e6:.0f} MB). "
            f"Pass --force to replace it."
        )

    # Built in a scratch file and renamed only on success. The dump load runs
    # with the rollback journal off, so an interrupted write leaves a
    # structurally broken file — this keeps that away from a usable database.
    work = target.with_suffix(target.suffix + ".building")
    for leftover in (work, Path(f"{work}-wal"), Path(f"{work}-shm")):
        leftover.unlink(missing_ok=True)

    started = time.monotonic()

    if args.mode == "test":
        with LinkDatabase(str(work)) as db:
            populate_test(db)
    elif args.mode == "empty":
        with LinkDatabase(str(work)) as db:
            db.set_meta("site", SUPPORTED_WIKIS[args.wiki][1])
            db.set_meta("wiki", args.wiki)
        print(f"Empty database for {SUPPORTED_WIKIS[args.wiki][1]}")
    else:
        load_dump(args.mode, work, Path(args.dumps), keep_staging=args.keep_staging)

    # A write-ahead log belongs to the database file it was written for. The
    # target's would be replayed into the new file, which SQLite reports as a
    # malformed image; the scratch file's would outlive the file it names.
    for stale in (
        Path(f"{target}-wal"), Path(f"{target}-shm"),
        Path(f"{work}-wal"), Path(f"{work}-shm"),
    ):
        stale.unlink(missing_ok=True)

    # Atomic on POSIX: no moment where the target is half-written.
    work.replace(target)
    print(f"\n{target} ready in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
