# wikiwalker

Finds the shortest path between two Wikipedia articles by following internal links.

## Setup

```bash
mkvirtualenv wikiwalker -a "$PWD" -p /opt/local/bin/python3.14
pip install -e ".[dev]"
pytest
pytest -m live   # ~70s at 10rps, compares the fetcher against the API on 30 random articles
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

`test` and `read-only` are testing scenarios. simplewiki is simple.wikipedia.org archive.

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

python3 walker.py read-only -v UK London           # source is a redirect
python3 walker.py read-only -v Bristol Uk          # target is a redirect
```

### Read-through demo

Forget one page, then watch the same walk answer from the network instead of disk.

```bash
python3 data_cli.py simplewiki
python3 walker.py read-only Bristol Cheese -v > run_1.log 2>&1

python3 data_cli.py forget --wiki simplewiki --title Cheshire
python3 walker.py read-only Bristol Cheese        # look at the WARNING

python3 walker.py crawl --db simplewiki Bristol Cheese -v > run_2.log 2>&1
diff run_1.log run_2.log
```

Probably you will observe a difference in total links for the page forgotten
and then re-crawled since the first count is upon a snapshot from the past.

### Crawling live Wikipedia

Ten requests per second by default, ten in flight.

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
walk stop and restart, or recover from a crash or a network outage. By definition,
the walk is over "stratified" data (layered in time).

`LinkStore.open_batch` builds a `BatchLinks` over the pages the store already
holds and the ones still arriving; `LinkStore.links_of` reads one title out of
it, so the `Walker` stays synchronous. Reading a page that has not landed blocks
until it has, which keeps the BFS loop free of any notion of fetching.

`BatchLinks` only reads. It answers with what the db held and what the fetcher
returned, and the store decides what to keep — so nothing but `LinkStore`
writes.

A page in the store is one of:

| status | meaning |
|---|---|
| `article` | the title served a page, and its links are in `links` |
| `redirect` | the title redirects to another, named in `redirect_to` |
| `notfound` | the title returned 404 |

A title with no row has never been read. `notfound` is a 24h cache of the 404,
so a crawl does not re-request it on every walk; the dump needs no such state,
since a complete snapshot proves absence on its own.

A link to a `notfound` title is a red link. red links are just counted during the
walk, the attribute "red link" is not stored anywhere.

Replace the db with an empty one:

```bash
python3 data_cli.py empty --wiki {simplewiki,wikipedia-us} --force
```

## Notes and trade-offs

simple.wikipedia.org has been added to facilitate tests (approx. 60 links per page vs 200-300).

### LinkStore

1. *Read skew*: a walk spans pages read at different instants, so the graph it traverses
    may never have existed as a whole.
2. An article, once stored, is **never refreshed**. The only refresh rule covers
   `notfound`, re-checked after 24h (`BatchLinks`).
3. `BatchLinks.drain()` collects the pages of the current batch even if the batch iteration is
    stopped. Closing the store uses `keep_what_landed()` instead, which keeps what
    has arrived and cancels the rest — most of a batch is still queued behind the
    concurrency cap, so waiting would start requests nobody will read.


### Fetcher

Redirect(s) work as on Wikipedia. A -> B returns B and does not count the extra hop.
If A -> B -> C: B is returned, and so the extra hop is counted. A wiki does not
HTTP-redirect: it serves the target's HTML under the alias's own URL, so the
redirect is read from `<link rel="canonical">` rather than from the response.

1. Wikimedia publishes no rate for `/wiki/` HTML — robots.txt sets a crawl
   delay for one named bot and otherwise asks that bots be "low-speed". The
   default of 10 rps is our choice, not theirs; `--max-rps` lowers it.

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

Even if a certain level of imperfection for link extraction is probably acceptable,
there is a unit test that compares results from API and from Fetcher to make sure
it divergence is <= 0.75% (pytest -m live).


### data_cli

1. `wikiparse-rs` for SQL → SQLite. `wikiwalk` / `wiki-graph` are full solutions,
    deliberately avoided.
2. Did not look at `wikiwalk`'s OO design, to build my own interpretation.


## TODO

+ add support multi paths
    Top 5 shortest paths - refers to the first 5 found shortest paths between a source and a target

+ There are still some layers of AI-powered spaghetti code, since there is no way to pass a high level
  design so that an agent can do it withour inventing funny stuff. I stopped digging into those
  spaghetti to put a limit on the time spent, reviewing the design until LinkStore.links_of and BatchLinks.

