# wikiwalker

Finds the shortest path between two Wikipedia articles by following internal links.

## Setup

```bash
mkvirtualenv wikiwalker -a "$PWD" -p /opt/local/bin/python3.14t
pip install -e ".[dev]"
pytest
```

```bash
python walker.py --help
```

Each scenario has its own contextual help:

```bash
python walker.py crawl --help
```

| scenario | store | what it does |
|---|---|---|
| `test` | `test.db` | synthetic 41-page wiki, no network |
| `read-only` | `simplewiki.db` | never fetches; needs `data_cli` to import the dump first |
| `crawl` | `wikipedia-us.db` | fetches what is missing over HTTP |

`test` and `read-only` are testing scenarios.

## Examples

The synthetic wiki needs no setup — `walker.py test` builds and crawls it.

| command | shows |
|---|---|
| `walker.py test -v L0 L3_26` | shortcut found at depth 2, not 3 |
| `walker.py test -v L0 Orphan` | exhausts the graph: 40 pages, batches of 3/10/26, `Red_Link` 404 → dead end |
| `walker.py test -v L3_00 L0` | leaf can't reach the root — no path, but complete |
| `walker.py test Red_Link L0` | red-link source: refused before any walk |

### Read-only

```bash
cargo install wikiparse-rs      # required: parses the MySQL dump
python3 data_cli.py simplewiki  # builds simplewiki.db (~2GB, downloads ~146MB)
```

```bash
python3 walker.py read-only Bristol Cheese
python3 walker.py read-only April Nigeria
python3 walker.py read-only "South West England" "Walton Cardiff"
```

### Read-through walkthrough

Forget one page, then watch the same walk answer from the network instead of disk.

```bash
python3 data_cli.py simplewiki
python3 walker.py read-only Bristol Cheese -v > run_1.log 2>&1

python3 data_cli.py forget --wiki simplewiki --title Cheshire
python3 walker.py read-only Bristol Cheese        # look at the WARNING

python3 walker.py crawl --db simplewiki Bristol Cheese -v > run_2.log 2>&1
diff run_1.log run_2.log
```

Same path either way, but with the hole the walk expands **127 pages instead of
25** and refuses to call the result shortest. The crawl then refills it from the
live wiki — and the live page has 78 links where the dump had 91 (or should...),
because it has been edited since the snapshot.

### Crawling live Wikipedia

One request per second by default.

```bash
python3 data_cli.py empty --wiki wikipedia-us
python3 walker.py crawl Bristol England
python3 walker.py crawl -v Bristol England --max-rps 0.5 --max-concurrency 3
```

## Design

A `Walker` walks the links from a `LinkStore`.

`LinkStore` is a read-through store: it returns a page's links from the db if
held, otherwise it asks a `Fetcher` for them. The fetcher retrieves the page —
over HTTP, or from a local sample wiki for tests — and extracts its links; the
store never sees HTML. The db, the network and the parsing are all encapsulated,
and none of that complexity reaches the `Walker`. The db is also what lets a
walk stop and restart, or recover from a crash or a network outage.

`LinkStore.get_links` returns a `BatchLinks` object: one mapping over the pages
the store already holds and the ones still arriving, so the `Walker` iterates it
synchronously. Reading a page that has not landed blocks until it has, which
keeps the BFS loop free of any notion of fetching.

A link is in one of three states:

| state | meaning |
|---|---|
| `article` | an article, so it has links to follow |
| `redlink` | no article behind the title, so nothing leads out of it |
| `unknown` | the store has never looked at it |

Replace the db with an empty one:

```bash
python3 data_cli.py empty --wiki {simplewiki,wikipedia-us} --force
```

## Notes and trade-offs

simple.wikipedia.org has been added to facilitate tests (approx. 60 links per page vs 200-300).

### LinkStore

1. An article, once stored, is **never refreshed**. The only refresh rule covers
   red links, re-checked after 24h (`LinkStore.get_links`).
2. `BatchLinks.drain()` — when looking for the shortest path first, it should
   arguably be cancelled rather than drained.
3. *Eventual consistency*: never a snapshot consistent at a given time. Whether
   a page is a red link, an orphan or an article is a past observation, so it
   may be stale. That state belongs in the walks table, not on the page.
4. Red links vs transient errors: TBD.

### data_cli

1. `wikiparse-rs` for SQL → SQLite. `wikiwalk` / `wiki-graph` are full
   solutions, deliberately avoided.
2. Did not look at `wikiwalk`'s OO design, to build my own interpretation.

### Fetcher

1. Concurrency of 10 is against Wikipedia's policies and might get you banned,
   so requests are also paced — at most one per second by default.
2. `429` is a **global pause**, not a per-request retry:

   ```
   429 -> pause the fetcher for Retry-After (all coroutines wait on one gate)
       -> resume
       -> if it happens N times, stop for real
   ```

3. `html.parser.HTMLParser` subclass, hrefs only: 3-5ms, against 50-100ms for
   BeautifulSoup. No full DOM is built, to cap CPU load.

Parser reliability:

1. `html.parser` is lenient, not spec-compliant. On malformed markup it can skip
   tags `lxml` would catch.
2. JS-loaded content is invisible — anything not in the served HTML. Collapsed
   navboxes are fine, being present but hidden.
3. HTML parsing is not an exact science; `html.parser` is chosen for simplicity.
4. Category pages are filtered out, being giant hubs rather than article links —
   by `id="mw-content-text"`, and by name prefix within the content div.

## TODO

1. Whether `extract_links` works on current en.wikipedia rendering. Only the
   sample wiki and simplewiki dumps have been exercised.
2. Redirect resolution emits duplicate edges — inflates `link_count()`, would
   skew most-frequent counts.
3. Whether 1 req/s clears Wikimedia's limits for `/wiki/` HTML. It comes from
   their general guidance, not a published number. On a 429, link to the policy.
