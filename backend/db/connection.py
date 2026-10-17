"""SQLite connection management for ASEC (Slice 2.1).

This module provides the only entry point for opening a database connection. It
never opens a database on import and holds no module-level (global) connection, so
callers always pass an explicit path.

Every connection has ``PRAGMA foreign_keys = ON`` set and verified, because the SRS
(§9, DR-01) requires authoritative state in a store with enforced references.
File-backed databases additionally use WAL journal mode so readers are not blocked
by a writer. WAL allows one writer at a time; it does not permit concurrent writers.

Connections are put in autocommit mode (``isolation_level = None``) and transactions
are controlled explicitly through :func:`transaction`, so schema and data changes can
be committed or rolled back as a unit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_MEMORY = ":memory:"
_BUSY_TIMEOUT_MS = 5000


class DatabaseError(Exception):
    """Raised when a database connection cannot be configured as required."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` with foreign keys enforced and return the connection.

    ``path`` is required; the caller decides which database to open. ``":memory:"``
    opens an in-memory database and skips WAL. A file-backed database has WAL journal
    mode enabled. Raises :class:`DatabaseError` if foreign keys or WAL cannot be
    enabled, and ``sqlite3.OperationalError`` if the file cannot be opened.
    """
    location = str(path)
    connection = sqlite3.connect(location)
    try:
        connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        if _foreign_keys_enabled(connection) != 1:
            raise DatabaseError("Failed to enable PRAGMA foreign_keys on this connection")
        if location != _MEMORY:
            mode = _journal_mode(connection, "WAL")
            if mode != "wal":
                raise DatabaseError(f"Failed to enable WAL journal mode (got {mode!r})")
    except Exception:
        connection.close()
        raise
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a single write transaction.

    Commits when the block completes and rolls back if it raises. ``BEGIN IMMEDIATE``
    takes the write lock up front so a competing writer cannot interleave. If
    ``COMMIT`` itself fails (for example a deferred foreign key violation), the
    transaction may still be open, so it is rolled back and the original error is
    re-raised unchanged.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        _rollback(connection)
        raise
    try:
        connection.execute("COMMIT")
    except BaseException:
        _rollback(connection)
        raise


def _rollback(connection: sqlite3.Connection) -> None:
    """Roll back, preserving whatever exception the caller is already handling."""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        # SQLite may already have ended the transaction (for example after a failed
        # COMMIT), so there may be nothing to roll back. Never mask the original error.
        pass


def _foreign_keys_enabled(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    if row is None:
        raise DatabaseError("Could not read the foreign_keys pragma")
    return int(row[0])


def _journal_mode(connection: sqlite3.Connection, mode: str) -> str:
    row = connection.execute(f"PRAGMA journal_mode = {mode}").fetchone()
    if row is None:
        raise DatabaseError("Could not read the journal_mode pragma")
    return str(row[0]).lower()
