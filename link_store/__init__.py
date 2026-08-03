"""Where a Walker gets its links.

    base.py     the LinkStore interface
    db.py       LinkDatabase — a link store on SQLite
    caching.py  CachingLinkStore — a link store backed by a fetcher

Two implementations, and the difference between them is what an unknown title
means. To `LinkDatabase` it is a gap in a loaded snapshot; to `CachingLinkStore`
it is a page nobody has asked for yet.
"""

from .base import LinkStore
from .caching import CachingLinkStore
from .db import RED_LINK_TTL_S, SCHEMA, LinkDatabase

__all__ = [
    "RED_LINK_TTL_S",
    "SCHEMA",
    "CachingLinkStore",
    "LinkDatabase",
    "LinkStore",
]
