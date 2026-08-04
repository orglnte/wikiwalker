"""Where a Walker gets its links.

    store.py    LinkStore — holds links, retrieves what it does not hold
    db.py       LinkDatabase — the SQLite behind it
    fetcher.py  LinkFetcher — concurrent retrieval, a future per page
    batch.py    BatchLinks — one batch's answer, resolved on access

`LinkStore` is the only one a search needs. Whether a page came off disk or off
the wire is not visible from outside it.
"""

from .batch import MISSING, PAGE_FETCH_FAILED, BatchLinks, Entry
from .db import SCHEMA, LinkDatabase, PageType
from .fetcher import LinkFetcher
from .store import DB_FILE, LinkStore

__all__ = [
    "DB_FILE",
    "SCHEMA",
    "MISSING",
    "PAGE_FETCH_FAILED",
    "BatchLinks",
    "Entry",
    "LinkDatabase",
    "LinkFetcher",
    "LinkStore",
    "PageType",
]
