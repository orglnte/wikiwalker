"""Shared fixtures.

Every test runs against a real `LinkDatabase` on an in-memory SQLite file, not
a mock. The behaviour under test *is* the SQL — the status filter in
`get_links`, the batching around the host-parameter limit, the atomicity of
`store` — and a mock would assert that the fake behaves like the fake.

In-memory keeps it fast enough that isolation per test costs nothing.
"""

from __future__ import annotations

import pytest

from link_store import LinkDatabase


@pytest.fixture
def db() -> LinkDatabase:
    """An empty database, discarded when the test ends."""
    with LinkDatabase(":memory:") as database:
        yield database


@pytest.fixture
def graph(db: LinkDatabase) -> LinkDatabase:
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
    db.mark_red_links(["Nowhere"])
    return db
