"""W9 H4: SQLite busy timeout and bounded BEGIN IMMEDIATE retry.

Only the acquisition of the write transaction is retried, and only while SQLite
reports "database is locked". A statement failure after BEGIN is never retried:
a partially executed transaction must not be replayed.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from backend.config import ConfigError, load_config, load_sqlite_settings
from backend.db.connection import connect, transaction


def test_sqlite_settings_default_and_override() -> None:
    assert load_sqlite_settings(load_config({})).busy_timeout_ms == 5000
    assert (
        load_sqlite_settings(load_config({"SQLITE_BUSY_TIMEOUT_MS": "250"}))
        .busy_timeout_ms
        == 250
    )


@pytest.mark.parametrize("value", ["0", "-5", "soon"])
def test_invalid_sqlite_busy_timeout_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        load_sqlite_settings(load_config({"SQLITE_BUSY_TIMEOUT_MS": value}))


def test_connect_applies_the_configured_busy_timeout(tmp_path: Path) -> None:
    connection = connect(tmp_path / "timeout.db", busy_timeout_ms=250)
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        assert row[0] == 250
    finally:
        connection.close()


def _prepare(path: Path) -> None:
    connection = connect(path)
    try:
        connection.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
    finally:
        connection.close()


def test_second_writer_waits_for_a_held_lock_then_succeeds(tmp_path: Path) -> None:
    path = tmp_path / "contended.db"
    _prepare(path)
    holder = connect(path, busy_timeout_ms=5000)
    holder.execute("BEGIN IMMEDIATE")
    result: dict[str, object] = {}
    started = time.monotonic()

    def write() -> None:
        writer = connect(path, busy_timeout_ms=5000)
        try:
            with transaction(writer):
                writer.execute(
                    "INSERT INTO notes (id, body) VALUES (1, 'kept')"
                )
            result["ok"] = True
        except Exception as error:  # noqa: BLE001 - reported to the assertion
            result["error"] = error
        finally:
            writer.close()

    thread = threading.Thread(target=write)
    thread.start()
    time.sleep(0.3)
    holder.execute("COMMIT")
    holder.close()
    thread.join(timeout=5)

    assert result.get("ok") is True, result.get("error")
    assert time.monotonic() - started >= 0.25


def test_lock_held_past_the_retries_raises_the_existing_error(tmp_path: Path) -> None:
    path = tmp_path / "locked.db"
    _prepare(path)
    holder = connect(path, busy_timeout_ms=5000)
    holder.execute("BEGIN IMMEDIATE")
    writer = connect(path, busy_timeout_ms=50)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with transaction(writer):
                writer.execute("INSERT INTO notes (id, body) VALUES (1, 'lost')")
    finally:
        writer.close()
        holder.execute("COMMIT")
        holder.close()


class RecordingConnection:
    """Minimal connection double: BEGIN succeeds, every later statement fails."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str) -> object:
        self.calls.append(sql)
        if sql == "BEGIN IMMEDIATE":
            return object()
        raise sqlite3.OperationalError("database is locked")


def test_failure_after_begin_is_never_retried() -> None:
    connection = RecordingConnection()

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with transaction(connection):  # type: ignore[arg-type]
            connection.execute("INSERT INTO notes (id, body) VALUES (1, 'x')")

    assert connection.calls.count("BEGIN IMMEDIATE") == 1
