"""Schema-level tests for the 0009_announcements migration (Slice 2.10) as
rebuilt by 0018_announcement_audience (Phase 7, PB-6).

Verifies: migration application/idempotency; accepted-announcement storage
per FR-REV-10, FR-REV-01 and §4.1 "Announcement": every accepted proposal is
accepted by an identified person at a verbatim timestamp, with the audience
recorded as a two-token type plus the audience vertical and, for an AH's
volunteers, the snapshot of the sender whose volunteers are addressed.

Schema-level acceptance integrity is UNIQUE (proposal_id): a proposal can be
the origin of at most one accepted record. The task/announcement XOR —
exactly one of the two per accepted proposal (FR-REV-05, AC-18) — spans the
2.7 task table and this table and is therefore a service-level guarantee;
the schema side lives in task UNIQUE (proposal_id) (Slice 2.7).

Out of scope and not claimed: the accept endpoint or authorization, review
state transitions, audience resolution or display, notifications, and what
the SRS leaves to the service layer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_ANNOUNCEMENT_TABLE = "announcement"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row["version"] for row in rows]


def _applied_at(connection: sqlite3.Connection, version: int) -> str:
    row = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    assert row is not None
    return str(row["applied_at"])


def _add_vertical(connection: sqlite3.Connection, vertical_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, f"vertical-{vertical_id}")
    )
    return vertical_id


def _add_group(connection: sqlite3.Connection, group_id: int = 1, vertical_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (?, ?, ?)",
        (group_id, f"ch-{group_id}", vertical_id),
    )
    return group_id


def _add_message(
    connection: sqlite3.Connection,
    message_id: int = 1,
    group_id: int = 1,
) -> int:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id, received_at)"
        " VALUES (?, 'discord', ?, ?, '2026-05-01T10:00:00Z')",
        (message_id, str(message_id), group_id),
    )
    return message_id


def _add_bucket(connection: sqlite3.Connection, bucket_id: int = 1, group_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO bucket (id, group_id) VALUES (?, ?)", (bucket_id, group_id)
    )
    return bucket_id


def _join_message(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    message_id: int = 1,
    group_id: int = 1,
) -> int:
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id) VALUES (?, ?, ?)",
        (bucket_id, message_id, group_id),
    )
    return message_id


def _add_person(connection: sqlite3.Connection) -> int:
    # PB-5: mandatory unique synthetic phone/roll_number for the roster row.
    cursor = connection.execute(
        "INSERT INTO person (name, phone, role, roll_number)"
        " VALUES ('reviewer', 'ph-reviewer', 'AH', 'roll-reviewer')"
    )
    return int(cursor.lastrowid or 0)


def _attempt(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    *,
    stage: str = "proposal_generation",
    outcome: str = "SUCCEEDED",
    started_at: str = "2026-05-01T11:00:00Z",
) -> int:
    cursor = connection.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at, outcome)"
        " VALUES (?, ?, ?, ?)",
        (stage, bucket_id, started_at, outcome),
    )
    return int(cursor.lastrowid or 0)


def _proposal(
    connection: sqlite3.Connection,
    attempt_id: int = 1,
    bucket_id: int = 1,
    primary_message_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "attempt_id": attempt_id,
        "bucket_id": bucket_id,
        "primary_message_id": primary_message_id,
        "source_vertical_id": 1,
        "ptype": "ANNOUNCEMENT",
        "assignment_mode": "NONE",
        "task_type": "INTERNAL",
        "structurally_valid": 1,
        "generated_at": "2026-05-01T11:05:00Z",
        "model_id": "model-p",
        "model_version": "1.0.0",
        "prompt_version": "prompt-1",
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO proposal ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _announcement(
    connection: sqlite3.Connection,
    proposal_id: int = 1,
    accepted_by: int = 1,
    *,
    accepted_at: str = "2026-05-01T12:00:00Z",
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "accepted_by": accepted_by,
        "accepted_at": accepted_at,
        # PB-6/FR-REV-10: structured audience replaces the free-text scope.
        "audience_type": "VERTICAL",
        "audience_vertical_id": 1,
        "audience_ah_person_id": None,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO announcement ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


def _seed_accepted_proposal(connection: sqlite3.Connection) -> int:
    """Vertical + group + bucketed message + run attempt + accepted-ready proposal."""
    _add_vertical(connection)
    _add_group(connection)
    _add_bucket(connection)
    _add_message(connection)
    _join_message(connection)
    _add_person(connection)
    _attempt(connection)
    _proposal(connection)
    return 1


def test_default_migrations_create_announcement_table(conn: sqlite3.Connection) -> None:
    assert {_ANNOUNCEMENT_TABLE} <= _table_names(conn)
    assert {"proposal", "processing_attempt", "bucket_message"} <= _table_names(conn)
    assert {1, 2, 3, 4, 5, 9} <= set(_versions(conn))


def test_reapplying_the_migrations_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        assert {1, 2, 3, 4, 5, 9} <= set(_versions(connection))
        at_9 = _applied_at(connection, 9)
        assert apply_migrations(connection) == []
        assert _versions(connection) == sorted(set(_versions(connection)))
        assert _applied_at(connection, 9) == at_9
    finally:
        connection.close()


def test_migrations_recording_versions_are_ordered_and_include_0009(
    conn: sqlite3.Connection,
) -> None:
    versions = _versions(conn)
    assert versions == sorted(set(versions))
    assert {1, 2, 3, 4, 5, 9} <= set(versions)


def test_announcement_minimum_fields_stored_verbatim(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    announcement_id = _announcement(
        conn,
        accepted_at="2026-05-01T12:45:00Z",
        audience_type="VERTICAL",
    )
    row = _row(conn, "announcement", announcement_id)
    assert row["proposal_id"] == 1
    assert row["accepted_by"] == 1
    assert row["accepted_at"] == "2026-05-01T12:45:00Z"
    # PB-6: the audience is the two-token type plus a mandatory vertical id.
    assert row["audience_type"] == "VERTICAL"
    assert row["audience_vertical_id"] == 1
    assert row["audience_ah_person_id"] is None


def test_announcement_named_vertical_audience(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    announcement_id = _announcement(
        conn,
        accepted_by=1,
        audience_vertical_id=1,
        audience_type="VERTICAL",
    )
    row = _row(conn, "announcement", announcement_id)
    assert row["audience_vertical_id"] == 1
    assert row["audience_type"] == "VERTICAL"


def test_announcement_unknown_references_rejected(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _announcement(conn, proposal_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _announcement(conn, accepted_by=999)
    with pytest.raises(sqlite3.IntegrityError):
        _announcement(conn, audience_vertical_id=999)


def test_announcement_required_fields_not_null(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    for fields in (
        {"accepted_by": None},
        {"accepted_at": None},
        {"audience_type": None},
        {"audience_vertical_id": None},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _announcement(conn, **fields)


def test_announcement_blank_fields_rejected(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    for fields in (
        {"accepted_at": ""},
        {"accepted_at": " "},
        {"audience_type": ""},
        {"audience_type": " "},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _announcement(conn, **fields)


def test_one_announcement_per_proposal(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    _announcement(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _announcement(conn, accepted_by=1)
    rows = conn.execute("SELECT COUNT(*) FROM announcement").fetchone()
    assert rows[0] == 1


def test_two_proposals_each_get_one_announcement(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    _announcement(conn, proposal_id=1)
    _add_message(conn, message_id=2)
    _join_message(conn, bucket_id=1, message_id=2)
    _proposal(conn, primary_message_id=2)
    _announcement(conn, proposal_id=2)
    rows = conn.execute("SELECT proposal_id FROM announcement ORDER BY proposal_id").fetchall()
    assert [row["proposal_id"] for row in rows] == [1, 2]


def test_deletion_restricted(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    _announcement(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM vertical WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM proposal WHERE id = 1")


def test_foreign_key_check_clean_after_workload(conn: sqlite3.Connection) -> None:
    _seed_accepted_proposal(conn)
    _announcement(conn, accepted_at="2026-05-01T12:10:00Z")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
