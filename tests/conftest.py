"""Shared fixtures.

Everything runs against real objects on an in-memory SQLite file, not mocks.
The behaviour under test *is* the SQL and the store's own bookkeeping, and a
mock would only assert that the fake behaves like the fake.
"""

from __future__ import annotations

import pytest

from link_store import LinkDatabase, LinkStore


@pytest.fixture
def sql() -> LinkDatabase:
    """The SQLite layer on its own."""
    with LinkDatabase(":memory:") as database:
        yield database


@pytest.fixture
def db() -> LinkStore:
    """An empty store with nothing to retrieve from."""
    with LinkStore(":memory:") as store:
        yield store


@pytest.fixture
def graph(db: LinkStore) -> LinkStore:
    """A small hand-built wiki covering every case the walker must distinguish.

        Source ──► Middle ──────────────► Target
          │           │
          │           ├──► Nowhere          (red link: no article behind it)
          │           └──► Unfetched        (unknown: never fetched at all)
          │
          ├──► Detour ──► Longer ──► Target (a second, longer route)
          └──► Barren                       (a real article linking nowhere)

    Isolated has no edges in either direction, so it is unreachable.
    """
    db.store("Source", ["Middle", "Detour", "Barren"])
    db.store("Middle", ["Target", "Nowhere", "Unfetched"])
    db.store("Detour", ["Longer"])
    db.store("Longer", ["Target"])
    db.store("Target", [])
    db.store("Barren", [])
    db.store("Isolated", [])
    db.mark_not_found(["Nowhere"])
    return db
