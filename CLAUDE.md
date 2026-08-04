# wikiwalker

Finds the shortest path between two Wikipedia articles by following internal
links. Breadth-first over a store of page links, filled from a Wikimedia dump
or by crawling.

## Setup

```
mkvirtualenv wikiwalker -a "$PWD" -p /opt/local/bin/python3.12
pip install -e ".[dev]"
pytest                       # 57 tests, no network
```

The CLIs run without installing anything: `python3 walker.py --mode test L0 L3_26`.

## Layout

```
walker.py         Walker (BFS) + WalkResult + the search CLI
data_cli.py       builds a database: test | simplewiki | enwiki | empty
scrape_cli.py     manual probe: fetch one live page, print its links

link_store/       base.py  LinkStore protocol
                  db.py    LinkDatabase — a link store on SQLite
                  caching.py CachingLinkStore — wraps a store + a fetcher

wikifetcher/      titles.py  what a title is; URL <-> title; namespaces
                  html_links.py  find <a href> in page HTML
                  base.py    Fetcher protocol
                  local.py   LocalFetcher — serves the synthetic wiki
                  sample_wiki.py  that wiki: 41 pages, 1 -> 3 -> 9 -> 27
```

## The two abstractions

`LinkStore` — an object that stores links. `Walker` walks one and cannot tell
which it got:

- `LinkDatabase` — SQLite. An unknown title is a gap in a loaded snapshot.
- `CachingLinkStore` — wraps any store plus a `Fetcher`. An unknown title just
  means nobody has asked yet, so asking fills it.

`Fetcher` — titles in, the titles they link to out, in batches so requests run
concurrently (cap 10). `LocalFetcher` serves the synthetic wiki; an HTTP one is
not written yet.

## Invariants worth not breaking

**Only `wikifetcher` knows this is a wiki.** URL forms, the title convention,
namespaces and HTML live there. `Walker` and `link_store` treat titles as
opaque names. The CLIs are composition roots and may import anything.

**Red link is not the same as gap.** A title with no article is a dead end that
costs the answer nothing; a title the store never fetched is a hole that makes
the result non-exhaustive. `WalkResult.complete` carries that distinction —
`found` says whether there is a path, `complete` says whether you can trust it
to be the shortest.

**The two ends of a search are not symmetric.** A source needs outgoing links
so it must be a real article; a target only needs to be linked to, so a red
link is reachable and is reported, not refused.

**Bulk load and crawl must produce identical databases.** `data_cli.py test`
writes `sample_wiki.GRAPH` directly; `walker.py --mode test` renders the same
graph to HTML and re-derives it through the real parser. Equality is therefore
an assertion about `html_links.py`. Check it after touching either:

```
python3 data_cli.py test --db tmp/bulk.db
python3 data_cli.py empty --db tmp/crawl.db
python3 walker.py --mode test --db tmp/crawl.db L0 Orphan
# then compare pages and links tables
```

## Conventions

- **batch**, not chunk, for a group of pages read or fetched in one call.
- Domain vocabulary stays: pages, links, titles, articles — not nodes.
  Decoupling means removing imports, never removing meaning.
- Comments are 1-2 lines and explain what the code cannot say itself. They
  never reference session history, prior versions, or the exercise brief.
- Numbers in prose are measured, not estimated.

## Databases

One per dataset, all gitignored — rebuild rather than expect them present.

| command | file |
|---|---|
| `data_cli.py test` | `test.db` (instant) |
| `data_cli.py simplewiki` | `simplewiki.db` (~1.8 GB, needs `cargo install wikiparse-rs`) |
| `data_cli.py enwiki` | `wikipedia-us.db` (much larger, unverified) |

`data_cli.py` refuses to overwrite an existing database without `--force`.
`walker.py --mode` picks the store: read -> simplewiki.db, test -> test.db,
crawl -> wikipedia-us.db. `--db` overrides it.

`tests/test_integration.py` skips itself when `simplewiki.db` is absent.

## Known limitations

- Namespace filtering lists English prefixes only, so `data_cli.py` accepts
  only `enwiki` and `simplewiki`.
- A crawl traverses a graph that never existed at one instant — pages change
  while it walks. A returned path can be re-verified; optimality cannot.
- Redirect resolution can produce duplicate edges for one page, which would
  skew a most-frequent-article count.
- `ruff` and `mypy` are configured but have not been run.

## Not built yet

HTTP fetcher; persisting every walked path; the top-5 shortest and top-5
most-frequent queries.
