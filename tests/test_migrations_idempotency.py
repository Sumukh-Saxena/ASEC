"""0025 idempotency schema (owner A2, W3).

Pins the unique key, the FK to the roster, the key-length window and the fact
that the table can hold one stored response per (person, route, key). The
migration applies once and re-applies as a no-op.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_PERSON_ROLL = "R-VOL"
_PERSON_DISCORD = "100000000000000004"
_NOW = "2026-11-13T09:00:00+00:00"
_LATER = "2026-11-14T09:00:00+00:00"
_HASH = "a" * 64


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "idempotency_schema.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row["version"]) for row in rows]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]): dict(row) for row in rows}


def _add_person(connection: sqlite3.Connection, person_id: int = 1) -> int:
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'Logistics')")
    connection.execute(
        "INSERT INTO person"
        " (id, name, discord_user_id, phone, role, active, roll_number)"
        " VALUES (?, 'Volunteer One', ?, 'ph-1', 'Volunteer', 1, ?)",
        (person_id, _PERSON_DISCORD, _PERSON_ROLL),
    )
    return person_id


def _insert(
    connection: sqlite3.Connection,
    *,
    person_id: int = 1,
    route_key: str = "TASK_CLAIM",
    key: str = "api-key-0123456789abcdef",
    request_hash: str = _HASH,
    response_status: int = 201,
    response_body: str = '{"task_id":1}',
) -> None:
    connection.execute(
        "INSERT INTO idempotency_key (person_id, route_key, key, request_hash,"
        " response_status, response_body, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            route_key,
            key,
            request_hash,
            response_status,
            response_body,
            _NOW,
            _LATER,
        ),
    )


def test_0025_applies_once_and_reapplies_as_a_no_op(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == list(range(1, 27))
    assert "idempotency_key" in _table_names(conn)
    assert apply_migrations(conn) == []
    assert _versions(conn) == list(range(1, 27))


def test_the_table_has_exactly_the_planned_columns(conn: sqlite3.Connection) -> None:
    assert set(_columns(conn, "idempotency_key")) == {
        "id",
        "person_id",
        "route_key",
        "key",
        "request_hash",
        "response_status",
        "response_body",
        "created_at",
        "expires_at",
    }


def test_person_route_and_key_are_unique(conn: sqlite3.Connection) -> None:
    _add_person(conn)
    _insert(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn)


def test_two_people_may_use_the_same_key(conn: sqlite3.Connection) -> None:
    _add_person(conn, 1)
    conn.execute(
        "INSERT INTO person"
        " (id, name, phone, role, active, roll_number)"
        " VALUES (2, 'Volunteer Two', 'ph-2', 'Volunteer', 1, 'R-VOL2')"
    )
    _insert(conn, person_id=1)
    _insert(conn, person_id=2)
    assert conn.execute("SELECT COUNT(*) AS n FROM idempotency_key").fetchone()["n"] == 2


@pytest.mark.parametrize("key", ["short", "x" * 65])
def test_the_key_length_window_is_enforced(
    conn: sqlite3.Connection, key: str
) -> None:
    _add_person(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, key=key)


def test_the_route_key_and_body_must_not_be_blank(conn: sqlite3.Connection) -> None:
    _add_person(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, route_key="   ")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, response_body="")


def test_an_unknown_person_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, person_id=99)


def test_the_expiry_index_exists(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name ="
        " 'idempotency_key'"
    ).fetchall()
    names = {str(row["name"]) for row in rows}
    assert "ix_idempotency_key_expiry" in names
