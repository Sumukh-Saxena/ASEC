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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.config import DEFAULT_SQLITE_BUSY_TIMEOUT_MS

_MEMORY = ":memory:"
#: Kept as a module-level default for callers that read or override it in tests;
#: production passes the validated ``SQLITE_BUSY_TIMEOUT_MS`` value explicitly.
_BUSY_TIMEOUT_MS = DEFAULT_SQLITE_BUSY_TIMEOUT_MS
_BEGIN_RETRY_DELAYS = (0.05, 0.1, 0.2)


class DatabaseError(Exception):
    """Raised when a database connection cannot be configured as required."""


def connect(
    path: str | Path,
    *,
    busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Open ``path`` with foreign keys enforced and return the connection.

    ``path`` is required; the caller decides which database to open. ``":memory:"``
    opens an in-memory database and skips WAL. A file-backed database has WAL journal
    mode enabled. ``busy_timeout_ms`` (W9 H4) is the validated configuration value
    applied to this connection; ``None`` uses the module default. Raises
    :class:`DatabaseError` if foreign keys or WAL cannot be enabled, and
    ``sqlite3.OperationalError`` if the file cannot be opened.
    """
    effective_timeout = _BUSY_TIMEOUT_MS if busy_timeout_ms is None else busy_timeout_ms
    if (
        isinstance(effective_timeout, bool)
        or not isinstance(effective_timeout, int)
        or effective_timeout < 1
    ):
        raise ValueError(
            "busy_timeout_ms must be a positive integer,"
            f" got {effective_timeout!r}"
        )
    location = str(path)
    connection = sqlite3.connect(location)
    try:
        connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        connection.execute(f"PRAGMA busy_timeout = {effective_timeout}")
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
    takes the write lock up front so a competing writer cannot interleave. Only the
    acquisition of that lock is retried (W9 H4): when SQLite reports "database is
    locked" it is attempted up to three more times with 50/100/200 ms backoff. A
    statement failure after BEGIN is never retried. If ``COMMIT`` itself fails (for
    example a deferred foreign key violation), the transaction may still be open, so
    it is rolled back and the original error is re-raised unchanged.
    """
    _begin_immediate(connection)
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


def _begin_immediate(connection: sqlite3.Connection) -> None:
    """Acquire the write lock, retrying only a locked BEGIN IMMEDIATE."""
    attempts = 0
    while True:
        try:
            connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as error:
            if not _is_locked(error) or attempts >= len(_BEGIN_RETRY_DELAYS):
                raise
            time.sleep(_BEGIN_RETRY_DELAYS[attempts])
            attempts += 1


def _is_locked(error: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(error).lower()


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
