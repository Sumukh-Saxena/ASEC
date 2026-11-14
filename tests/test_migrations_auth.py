"""0024 auth schema: OTP challenges, sessions and rate-limit evidence (W1).

FR-ROS-03 and FR-AUT-01 need an OTP challenge that stores only a salted hash, a
session that stores only a token hash, and a rate-limit ledger keyed on a roll
number hash. This module pins the migration constraints and the secret rule:
no table has a column that can hold a plaintext code or token.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_NEW_TABLES = ("otp_challenge", "auth_session", "auth_attempt")
_PERSON_ROLL = "R-VOL"
_PERSON_DISCORD = "100000000000000004"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "auth_schema.db")
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


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _add_person(connection: sqlite3.Connection) -> int:
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'Logistics')")
    connection.execute(
        "INSERT INTO person"
        " (id, name, discord_user_id, phone, role, active, roll_number)"
        " VALUES (1, 'Volunteer One', ?, 'ph-1', 'Volunteer', 1, ?)",
        (_PERSON_DISCORD, _PERSON_ROLL),
    )
    return 1


def test_0024_applies_once_and_reapplies_as_a_no_op(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == list(range(1, 25))
    assert set(_NEW_TABLES) <= _table_names(conn)
    assert apply_migrations(conn) == []
    assert _versions(conn) == list(range(1, 25))


def test_0024_has_no_plaintext_code_or_token_column(conn: sqlite3.Connection) -> None:
    assert _columns(conn, "otp_challenge") == {
        "id",
        "person_id",
        "code_salt",
        "code_hash",
        "created_at",
        "expires_at",
        "attempts",
        "consumed_at",
    }
    assert _columns(conn, "auth_session") == {
        "id",
        "person_id",
        "token_hash",
        "issued_at",
        "expires_at",
        "revoked_at",
    }
    assert _columns(conn, "auth_attempt") == {
        "id",
        "roll_number_hash",
        "kind",
        "attempted_at",
        "succeeded",
        "reason",
    }


def test_otp_challenge_rejects_blank_hash_material(conn: sqlite3.Connection) -> None:
    person_id = _add_person(conn)
    statement = (
        "INSERT INTO otp_challenge"
        " (person_id, code_salt, code_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, '2026-11-13T09:00:00+00:00',"
        " '2026-11-13T09:05:00+00:00')"
    )
    for salt, code_hash in (("", "abc"), ("   ", "abc"), ("abc", ""), ("abc", "  ")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, (person_id, salt, code_hash))


def test_otp_challenge_attempts_and_person_are_constrained(
    conn: sqlite3.Connection,
) -> None:
    person_id = _add_person(conn)
    statement = (
        "INSERT INTO otp_challenge"
        " (person_id, code_salt, code_hash, created_at, expires_at, attempts)"
        " VALUES (?, 'aa', 'bb', '2026-11-13T09:00:00+00:00',"
        " '2026-11-13T09:05:00+00:00', ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement, (person_id, -1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement, (999, 0))


def test_session_token_hash_is_unique_and_never_blank(conn: sqlite3.Connection) -> None:
    person_id = _add_person(conn)
    statement = (
        "INSERT INTO auth_session"
        " (person_id, token_hash, issued_at, expires_at)"
        " VALUES (?, ?, '2026-11-13T09:00:00+00:00',"
        " '2026-11-20T09:00:00+00:00')"
    )
    conn.execute(statement, (person_id, "hash-one"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement, (person_id, "hash-one"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement, (person_id, "   "))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement, (999, "hash-two"))


def test_auth_attempt_succeeded_is_boolean_and_text_is_required(
    conn: sqlite3.Connection,
) -> None:
    statement = (
        "INSERT INTO auth_attempt"
        " (roll_number_hash, kind, attempted_at, succeeded, reason)"
        " VALUES (?, ?, ?, ?, ?)"
    )
    conn.execute(statement, ("hash", "OTP_REQUEST", "2026-11-13T09:00:00+00:00", 1, None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            statement, ("hash", "OTP_REQUEST", "2026-11-13T09:00:00+00:00", 2, None)
        )
    for values in (
        ("", "OTP_REQUEST", "2026-11-13T09:00:00+00:00", 1, None),
        ("hash", "  ", "2026-11-13T09:00:00+00:00", 1, None),
        ("hash", "OTP_REQUEST", "", 1, None),
        ("hash", "OTP_REQUEST", "2026-11-13T09:00:00+00:00", 1, "  "),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, values)


def test_auth_tables_keep_integrity_and_foreign_keys_clean(
    conn: sqlite3.Connection,
) -> None:
    person_id = _add_person(conn)
    conn.execute(
        "INSERT INTO otp_challenge"
        " (person_id, code_salt, code_hash, created_at, expires_at)"
        " VALUES (?, 'aa', 'bb', '2026-11-13T09:00:00+00:00',"
        " '2026-11-13T09:05:00+00:00')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO auth_session"
        " (person_id, token_hash, issued_at, expires_at)"
        " VALUES (?, 'hash-one', '2026-11-13T09:00:00+00:00',"
        " '2026-11-20T09:00:00+00:00')",
        (person_id,),
    )
    conn.execute(
        "INSERT INTO auth_attempt"
        " (roll_number_hash, kind, attempted_at, succeeded)"
        " VALUES ('hash', 'VERIFY', '2026-11-13T09:00:00+00:00', 0)"
    )

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()] == [
        "ok"
    ]


def test_0024_adds_the_indexes_the_rate_limit_and_lookup_use(
    conn: sqlite3.Connection,
) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    names = {str(row["name"]) for row in rows}

    assert "ix_otp_challenge_person" in names
    assert "ix_auth_session_person" in names
    assert "ix_auth_attempt_roll" in names
