"""Tests for backend.db.connection (Slice 2.1 database foundation)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import backend.db.connection as connection_module
from backend.db.connection import connect, transaction


def test_foreign_keys_are_enabled(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    try:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()


def test_foreign_keys_reject_orphan_records(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES parent(id))"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
    finally:
        conn.close()


def test_wal_is_enabled_for_file_backed_databases(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0] == "wal"
    finally:
        conn.close()


def test_memory_database_skips_wal_but_enforces_foreign_keys() -> None:
    conn = connect(":memory:")
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
        assert journal is not None
        assert journal[0] == "memory"
        assert foreign_keys is not None
        assert foreign_keys[0] == 1
    finally:
        conn.close()


def test_connections_are_independent_objects(tmp_path: Path) -> None:
    first = connect(tmp_path / "shared.db")
    second = connect(tmp_path / "shared.db")
    try:
        assert first is not second
        first.close()
        assert second.execute("SELECT 1").fetchone() is not None
    finally:
        second.close()


def test_module_does_not_open_a_global_connection() -> None:
    connections = [v for v in vars(connection_module).values() if isinstance(v, sqlite3.Connection)]
    assert connections == []


def test_second_connection_sees_committed_writes(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    writer = connect(path)
    reader = connect(path)
    try:
        writer.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        writer.execute("INSERT INTO notes (id, body) VALUES (1, 'hello')")
        row = reader.execute("SELECT body FROM notes WHERE id = 1").fetchone()
        assert row is not None
        assert row[0] == "hello"
    finally:
        writer.close()
        reader.close()


def test_transaction_commits_on_success(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    try:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        with transaction(conn):
            conn.execute("INSERT INTO notes (id, body) VALUES (1, 'kept')")
        row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        conn.close()


def test_transaction_rolls_back_on_failure(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    try:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                conn.execute("INSERT INTO notes (id, body) VALUES (1, 'first')")
                conn.execute("INSERT INTO notes (id, body) VALUES (1, 'duplicate')")
        row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        conn.close()


def test_transaction_rolls_back_when_commit_fails(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES parent(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
        # A deferred foreign key makes COMMIT fail rather than the INSERT.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with transaction(conn):
                conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")

        # The connection must not be left inside a transaction and the row is gone.
        assert conn.in_transaction is False
        row = conn.execute("SELECT COUNT(*) FROM child").fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        conn.close()


def test_connect_to_missing_directory_raises_cleanly(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        connect(tmp_path / "does_not_exist" / "test.db")
