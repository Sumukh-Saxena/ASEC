"""Fresh-database foreign-key probe at schema version 11 (Slice A4-era note).

``0008`` declares ``announcement_id ... REFERENCES announcement``, but the
``announcement`` table is created by later migration ``0009``. SQLite resolves
foreign-key targets lazily at DML time, so the ordered fresh-database apply
works — this is the standing probe promised in ``docs/PHASE2_PLAN.md``
(Slice 2.9, "probe: versions [1..9]") extended to the full pre-A4 schema.

It is a note as a test: a fresh database migrated through version 11 (every
Phase 2 data-layer table, before the Slice A4 additions) has zero
``PRAGMA foreign_key_check`` violations. Migration ``0008`` itself is NOT
modified — renumbering or moving the table would churn committed names,
tests, and documentation for no behavioral gain; always apply the full
ordered manifest before any service writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations


@pytest.fixture()
def version_11_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh database migrated through version 11 only."""
    staging = tmp_path / "migrations"
    staging.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name[:4])
        if version <= 11:
            (staging / path.name).write_bytes(path.read_bytes())
    connection = connect(tmp_path / "fk11.db")
    apply_migrations(connection, directory=staging)
    yield connection
    connection.close()


def test_fresh_database_at_version_11_has_no_foreign_key_violations(
    version_11_conn: sqlite3.Connection,
) -> None:
    conn = version_11_conn
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert [row["version"] for row in rows] == list(range(1, 12))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
