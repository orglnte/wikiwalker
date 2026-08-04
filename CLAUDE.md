# wikiwalker — notes for an agent

Shortest path between two Wikipedia articles. **Read `README.md` first** — what
it is, how to run it, the design and its trade-offs all live there. This file is
only what you need to change the code without breaking it.

## Where things are

```
walker.py         Walker (BFS) + WalkResult + the search CLI
data_cli.py       builds a store: test | simplewiki | wikipedia-us | empty | forget
scrape_cli.py     manual probe: fetch one live page, print its links
settings.py       every operational knob, and nothing else
logctx.py         the walk's depth, for log lines written elsewhere

link_store/       store.py    LinkStore — holds links, retrieves what it lacks
                  db.py       LinkDatabase — the SQLite behind it
                  fetcher.py  LinkFetcher — an event loop on a thread, futures out
                  batch.py    BatchLinks — one batch's answer, resolved on access

wikifetcher/      titles.py       what a title is; URL <-> title; namespaces
                  html_links.py   find <a href> in page HTML
                  base.py         Fetcher protocol
                  local.py        LocalFetcher — serves the synthetic wiki
                  http.py         HttpFetcher — reads the real thing
                  sample_wiki.py  that wiki: 41 pages, 1 -> 3 -> 9 -> 27
```

## Checking a change

```
pytest                                    # 104 tests, no network
ruff check .
python3 walker.py test L0 L3_26           # no setup needed
```

After touching `html_links.py`, `sample_wiki.py` or either write path, check
that bulk load and crawl still agree:

```
python3 data_cli.py test --db tmp/bulk.db
python3 data_cli.py empty --db tmp/crawl.db --wiki simplewiki
python3 walker.py test --db tmp/crawl.db L0 Orphan
# then compare the pages and links tables
```

They must be identical. `data_cli.py test` writes `sample_wiki.GRAPH` directly;
`walker.py test` renders the same graph to HTML and re-derives it through the
real parser, so equality is an assertion about `html_links.py`.

## Invariants worth not breaking

**Only `wikifetcher` knows this is a wiki.** URL forms, the title convention,
namespaces and HTML live there. `Walker` and `link_store` treat titles as
opaque names. The CLIs are composition roots and may import anything.

**Three states, no more.** `article` (`status='ok'`), `redlink`
(`status='redlink'`), `unknown` (no row). All three are properties of the page
itself, so none change when some other page does. Anything derived from *other*
pages — orphan, reachable — is a past observation, not a stored fact.

**Red link is not the same as unread.** No article is a dead end that costs the
answer nothing; a page the store could not read is a hole that makes the result
non-exhaustive. `WalkResult.complete` carries the distinction — `found` says
whether there is a path, `complete` says whether to trust it as shortest.

**404 is an answer; anything else is a failure.** Confusing them stores a live
article as a red link, trusted as a dead end for 24h, and every walk through it
returns a longer path with `complete` still true. Wrong answer, no warning.

**The two ends of a search are not symmetric.** A source needs outgoing links so
it must be a real article; a target only needs to be linked to, so a red link is
reachable and is reported, not refused.

**`Fetcher` takes one title, not a batch.** A coroutine covering ten pages can
only resolve when the last lands, which defeats per-page blocking. `LinkFetcher`
owns the concurrency cap instead.

## Conventions

- **batch**, not chunk, for a group of pages read or fetched in one call.
  One depth = one frontier; a frontier is sliced into batches. A batch has no
  meaning to BFS — it bounds memory and query size.
- Domain vocabulary stays: pages, links, titles, articles — not nodes.
  Decoupling means removing imports, never removing meaning.
- Comments are 1-2 lines and explain what the code cannot say itself. They
  never reference session history, prior versions, or the exercise brief.
- Numbers in prose are measured, not estimated.

## Deliberate, do not "fix"

Each of these looks like an omission and is written up in `README.md`:

- an article is never refreshed; only red links expire (24h)
- `etag`, `db.touch()` and the general form of `stale_titles` have no callers
- a crawl's answer can never be provably optimal — the graph changes under it
- databases are gitignored; rebuild rather than expect them present
