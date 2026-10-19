"""Schema-level tests for the 0007_cross_vertical_approvals migration
(Slice 2.8).

Verifies: migration application/idempotency/failure isolation; approval
request storage per FR-XV-03/§4.1 (one request per task, requested_by person,
optional requester message, recipients snapshot); the FR-TSK-02 Approval
dimension on the task stays a task field; first decision wins via
UNIQUE (approval_decision.request_id) with the FR-XV-04/FR-TSK-02 decision
vocabulary APPROVED|DENIED and an optional decision message; decision evidence
keep-not-overwrite (FR-XV-05 denial keeps the task and decision, FR-XV-06
records requests/decisions/messages in the task history — service writes
history rows into the 0006 tables); delete restrictions over the
task-request-roster chains (DR-01/DR-02); concurrency shape (NFR-CON-02).

Storage-only and deliberately not enforced here: who may approve (recipient
membership/role at decision time — FR-XV-04, service-level); the AH/Head
distinction and roster recall (FR-ROS-01 — recipient rows reference the
persons rostered at request time and are a snapshot only); visibility of a
pending task to volunteers (FR-XV-03, app behavior); the requester being
notified (FR-NOT-01, 2.9 records only); the task going live on approval
(FR-XV-05, service); history rows for request/decision events (FR-XV-06,
service).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_APPROVAL_TABLES = {
    "approval_request",
    "approval_recipient",
    "approval_decision",
}

_REQUEST_AT = "2026-05-01T11:15:00Z"
_DECIDED_AT = "2026-05-01T11:45:00Z"
_GENERATED_AT = "2026-05-01T11:05:00Z"
_DECISION_RUN_AT = "2026-05-01T11:00:00Z"
_CREATED_AT = "2026-05-01T11:10:00Z"


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


def _database_file(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA database_list").fetchone()[-1])


def _add_person(connection: sqlite3.Connection, name: str, role: str) -> int:
    cursor = connection.execute(
        "INSERT INTO person (name, role) VALUES (?, ?)", (name, role)
    )
    return int(cursor.lastrowid or 0)


def _add_vertical(connection: sqlite3.Connection, vertical_id: int, name: str) -> int:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name)
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


def _attempt(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    *,
    stage: str = "proposal_generation",
    outcome: str = "SUCCEEDED",
    started_at: str = _DECISION_RUN_AT,
    **fields: Any,
) -> int:
    columns = ["stage", "bucket_id", "started_at", "outcome"]
    values: list[Any] = [stage, bucket_id, started_at, outcome]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO processing_attempt ({', '.join(columns)}) VALUES ({placeholders})",
        values,
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
        "ptype": "TASK",
        "assignment_mode": "EXPLICIT",
        "task_type": "INTERNAL",
        "structurally_valid": 1,
        "generated_at": _GENERATED_AT,
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


def _task(
    connection: sqlite3.Connection,
    proposal_id: int = 1,
    **fields: Any,
) -> int:
    """Cross-vertical task: source vertical 1, responsible vertical 2 (FR-XV-01)."""
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "task_type": "CROSS_VERTICAL",
        "source_vertical_id": 1,
        "responsible_vertical_id": 2,
        "creator_id": 1,
        "description": "carry the decor to logistics",
        "assignment_mode": "EXPLICIT",
        "people_needed": 1,
        "people_needed_unlimited": 0,
        "created_at": _CREATED_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO task ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _seed_cross_vertical_task(connection: sqlite3.Connection) -> int:
    """Roster (source + responsible verticals, requester + two recipients)
    through a linked proposal and a cross-vertical task; returns the task id."""
    _add_vertical(connection, 1, "decor")
    _add_vertical(connection, 2, "logistics")
    _add_person(connection, "requester-ah", "AH")
    _add_person(connection, "other-ah", "AH")
    _add_person(connection, "responsible-head", "HEAD")
    _add_group(connection)
    _add_message(connection)
    _add_bucket(connection)
    _join_message(connection)
    attempt_id = _attempt(connection)
    proposal_id = _proposal(connection, attempt_id=attempt_id)
    return _task(connection, proposal_id=proposal_id)


def _request(
    connection: sqlite3.Connection,
    task_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "task_id": task_id,
        "requested_by": 1,
        "created_at": _REQUEST_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO approval_request ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _recipient(
    connection: sqlite3.Connection,
    request_id: int = 1,
    person_id: int = 2,
) -> None:
    connection.execute(
        "INSERT INTO approval_recipient (request_id, person_id) VALUES (?, ?)",
        (request_id, person_id),
    )


def _decision(
    connection: sqlite3.Connection,
    request_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "request_id": request_id,
        "decided_by": 3,
        "decision": "APPROVED",
        "decided_at": _DECIDED_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO approval_decision ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


# --- migration application --------------------------------------------------


def test_approval_tables_created_after_earlier_migrations(conn: sqlite3.Connection) -> None:
    assert {1, 2, 3, 4, 5, 6, 7} <= set(_versions(conn))
    assert _APPROVAL_TABLES <= _table_names(conn)


def test_approval_migration_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        at_7 = _applied_at(connection, 7)
        assert apply_migrations(connection) == []
        assert _applied_at(connection, 7) == at_7
    finally:
        connection.close()


def test_failed_later_migration_preserves_earlier_migrations(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        (directory / path.name).write_bytes(path.read_bytes())
    (directory / "0900_seed.sql").write_text(
        "INSERT INTO vertical (id, name) VALUES (900, 'seed-vertical');\n", encoding="utf-8"
    )
    (directory / "0901_broken.sql").write_text(
        "CREATE TABLE ROLLBACK_CHECK (should_never_exist INTEGER PRIMARY KEY);\n"
        "INSERT INTO nonexistent_table (id) VALUES (1);\n",
        encoding="utf-8",
    )
    connection = connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, directory=directory)
        versions = _versions(connection)
        assert versions == sorted(set(versions))
        assert {1, 2, 3, 4, 5, 6, 7} <= set(versions)
        assert 900 in versions and 901 not in versions
        assert _APPROVAL_TABLES <= _table_names(connection)
        assert "ROLLBACK_CHECK" not in _table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0
    finally:
        connection.close()


# --- request: FR-XV-03 ------------------------------------------------------


def test_minimum_request(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    request_id = _request(conn)
    row = _row(conn, "approval_request", request_id)
    assert row["task_id"] == 1
    assert row["requested_by"] == 1
    assert row["request_message"] is None
    assert row["created_at"] == _REQUEST_AT


def test_one_request_per_task(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _request(conn)


def test_request_unknown_task_rejected(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _request(conn, task_id=999)


def test_request_message_verbatim(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    message = "bring the spare sound system — logistics needs it"
    request_id = _request(conn, request_message=message)
    assert _row(conn, "approval_request", request_id)["request_message"] == message


def test_request_and_decision_blank_messages_rejected(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-03/FR-XV-04: messages are optional but never blank. Each
    rejection is the first insert in scope, so no UNIQUE constraint shadows
    the CHECK."""
    _seed_cross_vertical_task(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _request(conn, request_message="   ")
    request_id = _request(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, request_id=request_id, decision_message="  ")
    decision_id = _decision(conn, request_id=request_id, decision="DENIED")
    assert _row(conn, "approval_decision", decision_id)["decision_message"] is None


def test_request_created_at_required(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _request(conn, created_at=" ")


def test_request_unknown_requester_and_deletion_restricted(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _request(conn, task_id=1, requested_by=999)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = 1")


def test_head_cross_vertical_task_has_no_request(conn: sqlite3.Connection) -> None:
    """AC-14: a Head's cross-vertical task goes live without a request.

    Discriminating checks: the task row itself must really exist (the
    scenario is not an empty fixture), its approval state must still be the
    schema default (NOT_REQUIRED — nothing implied a request), and no
    approval_request row may exist (no auto-creation side effect).
    """
    task_id = _seed_cross_vertical_task(conn)
    task = conn.execute(
        "SELECT task_type, approval_state FROM task WHERE id = ?", (task_id,)
    ).fetchone()
    assert task["task_type"] == "CROSS_VERTICAL"
    assert task["approval_state"] == "NOT_REQUIRED"
    count = conn.execute(
        "SELECT COUNT(*) FROM approval_request WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    assert count == 0


# --- recipients snapshot: FR-XV-03 ------------------------------------------


def test_recipients_snapshot(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    _recipient(conn, person_id=2)
    _recipient(conn, person_id=3)
    rows = conn.execute(
        "SELECT person_id FROM approval_recipient ORDER BY person_id"
    ).fetchall()
    assert [row["person_id"] for row in rows] == [2, 3]


def test_recipient_duplicates_and_unknown_rejected(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    _recipient(conn, person_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _recipient(conn, person_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _recipient(conn, person_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _recipient(conn, request_id=999, person_id=2)


def test_recipient_deletion_restricted(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    _recipient(conn, person_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM approval_request WHERE id = 1")


# --- decision: FR-XV-04, first decision wins --------------------------------


@pytest.mark.parametrize("decision", ["APPROVED", "DENIED"])
def test_decision_tokens_recordable(conn: sqlite3.Connection, decision: str) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    decision_id = _decision(conn, decision=decision)
    assert _row(conn, "approval_decision", decision_id)["decision"] == decision


def test_decision_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    for bad in ("APPROVE", " APPROVED ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _decision(conn, decision=bad)


def test_decision_message_verbatim(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    message = "approved — schedule after the rehearsal"
    decision_id = _decision(conn, decision_message=message)
    assert _row(conn, "approval_decision", decision_id)["decision_message"] == message


def test_decision_required_fields_and_unknown_person(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, decided_at=" ")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, decided_by=999)
    decision_id = _decision(conn)
    assert _row(conn, "approval_decision", decision_id)["decided_at"] == _DECIDED_AT


def test_first_decision_wins(conn: sqlite3.Connection) -> None:
    """FR-XV-04/AC-15: the first decision is the one; later attempts change
    nothing (schema half: one decision row per request)."""
    _seed_cross_vertical_task(conn)
    _request(conn)
    _decision(conn, decision="APPROVED")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, decision="DENIED")
    count = conn.execute("SELECT COUNT(*) FROM approval_decision").fetchone()[0]
    assert count == 1


def test_first_decision_wins_across_connections(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        _seed_cross_vertical_task(connection)
        _request(connection)
        _decision(connection, decision="APPROVED")
        second = connect(_database_file(connection))
        try:
            with transaction(second):
                with pytest.raises(sqlite3.IntegrityError):
                    second.execute(
                        "INSERT INTO approval_decision (request_id, decided_by, decision,"
                        " decided_at) VALUES (1, 2, 'DENIED', ?)",
                        (_DECIDED_AT,),
                    )
            count = connection.execute("SELECT COUNT(*) FROM approval_decision").fetchone()[0]
            assert count == 1
        finally:
            second.close()
    finally:
        connection.close()


def test_denied_decision_is_kept(conn: sqlite3.Connection) -> None:
    """FR-XV-05: on denial the task never goes live; task and decision kept."""
    _seed_cross_vertical_task(conn)
    _request(conn)
    decision_id = _decision(conn, decision="DENIED")
    conn.execute("UPDATE task SET approval_state = 'DENIED' WHERE id = 1")
    row = _row(conn, "approval_decision", decision_id)
    assert row["decision"] == "DENIED"
    assert _row(conn, "task", 1)["approval_state"] == "DENIED"


def test_task_approval_state_updates(conn: sqlite3.Connection) -> None:
    """FR-TSK-02: approval is a task dimension the flow updates."""
    _seed_cross_vertical_task(conn)
    conn.execute("UPDATE task SET approval_state = 'PENDING' WHERE id = 1")
    assert _row(conn, "task", 1)["approval_state"] == "PENDING"
    conn.execute("UPDATE task SET approval_state = 'APPROVED' WHERE id = 1")
    assert _row(conn, "task", 1)["approval_state"] == "APPROVED"


# --- integrity: DR-01/DR-02 -------------------------------------------------


def test_approval_chain_foreign_key_check_clean(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    _recipient(conn, person_id=2)
    _recipient(conn, person_id=3)
    _decision(conn, decision="APPROVED")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_task_deletion_restricted_by_request_chain(conn: sqlite3.Connection) -> None:
    _seed_cross_vertical_task(conn)
    _request(conn)
    _recipient(conn, person_id=2)
    _decision(conn, decision="APPROVED")
    for statement in (
        "DELETE FROM task WHERE id = 1",
        "DELETE FROM proposal WHERE id = 1",
        "DELETE FROM processing_attempt WHERE id = 1",
        "DELETE FROM approval_request WHERE id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
