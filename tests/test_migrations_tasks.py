"""Schema-level tests for the 0006_tasks migration (Slice 2.7).

Verifies: migration application/idempotency/failure isolation; task storage
per FR-TSK-01 (§3.6) with the FR-TSK-02 state dimensions held as independent
fields: assignment mode and cancellation and approval state on the task row,
agreement and work status on each assignee record (no single field encodes
more than one dimension); task type consistency with source/responsible
verticals (FR-XV-01); people-needed encoding (FR-TSK-01/04); assignee records
per FR-TSK-03 (§4.1 person, agreement, work status, timestamps); append-only
task history with actor attribution (FR-TSK-11, NFR-AUD-01/03); attribute and
affected-vertical links (FR-TSK-01/FR-PRP-05); delete restrictions across the
task-proposal-attempt-roster chains (DR-01/DR-02); concurrency shape
(NFR-CON-02).

Storage-only and deliberately not enforced here: transition legality and
COMPLETED-final for work status and AWAITING to ACCEPTED/DENIED for agreement
(FR-TSK-06/13 — service-level; the schema stores current values plus history
rows; SQLite CHECKs cannot compare rows over time and triggers are outside
the project's storage pattern); claim capacity against people needed
(FR-TSK-04/AC-08 — cross-row, service-level); assignee-record edits on stale
version rejection (FR-TSK-12 — the schema stores the version field the
service compares); authorization of changes (FR-TSK-14/16); notifications
(2.9 records only); approval requests and decisions (2.8).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_TASK_TABLES = {
    "task",
    "task_assignee",
    "task_history",
    "task_attribute",
    "task_affected_vertical",
}

_CREATED_AT = "2026-05-01T11:10:00Z"
_GENERATED_AT = "2026-05-01T11:05:00Z"
_DECISION_RUN_AT = "2026-05-01T11:00:00Z"


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


def _add_vertical(connection: sqlite3.Connection, vertical_id: int, name: str) -> int:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name)
    )
    return vertical_id


def _seed_roster(connection: sqlite3.Connection) -> None:
    _add_vertical(connection, 1, "logistics")
    _add_vertical(connection, 2, "decor")
    _add_person(connection, "creator", "AH")
    _add_person(connection, "volunteer-a", "VOLUNTEER")
    _add_person(connection, "volunteer-b", "VOLUNTEER")


def _add_person(
    connection: sqlite3.Connection,
    name: str,
    role: str,
) -> int:
    cursor = connection.execute(
        "INSERT INTO person (name, role) VALUES (?, ?)", (name, role)
    )
    return int(cursor.lastrowid or 0)


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


def _seed_generated_bucket(connection: sqlite3.Connection) -> int:
    """Roster plus one generated bucket whose primary message participates."""
    _seed_roster(connection)
    _add_group(connection)
    for message_id in (1, 2):
        _add_message(connection, message_id)
    _add_bucket(connection)
    _join_message(connection, bucket_id=1, message_id=1)
    _join_message(connection, bucket_id=1, message_id=2)
    return _attempt(connection)


def _seed_linked_proposal(connection: sqlite3.Connection, **fields: Any) -> int:
    attempt_id = _seed_generated_bucket(connection)
    return _proposal(connection, attempt_id=attempt_id, **fields)


def _task(
    connection: sqlite3.Connection,
    proposal_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "task_type": "INTERNAL",
        "source_vertical_id": 1,
        "responsible_vertical_id": None,
        "creator_id": 1,
        "description": "clip the wires",
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


def _task_from_accepted_proposal(
    connection: sqlite3.Connection,
    **fields: Any,
) -> int:
    proposal_id = _seed_linked_proposal(connection)
    return _task(connection, proposal_id=proposal_id, **fields)


def _assignee(
    connection: sqlite3.Connection,
    task_id: int = 1,
    person_id: int = 2,
    **fields: Any,
) -> None:
    record: dict[str, Any] = {
        "task_id": task_id,
        "person_id": person_id,
        "added_at": _CREATED_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO task_assignee ({', '.join(record)}) VALUES ({placeholders})", values
    )


def _history(
    connection: sqlite3.Connection,
    task_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "task_id": task_id,
        "field_name": "work_status",
        "old_value": "NOT_STARTED",
        "new_value": "COMPLETED",
        "changed_at": "2026-05-01T13:00:00Z",
        "actor_person_id": 2,
        "actor_model_id": None,
        "actor_model_version": None,
        "actor_role": "VOLUNTEER",
        "actor_vertical_id": 1,
        "reason": "performer marked the work completed",
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO task_history ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


# --- migration application --------------------------------------------------


def test_task_tables_created_after_earlier_migrations(conn: sqlite3.Connection) -> None:
    assert {1, 2, 3, 4, 5, 6} <= set(_versions(conn))
    assert _TASK_TABLES <= _table_names(conn)


def test_task_migration_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        at_6 = _applied_at(connection, 6)
        assert apply_migrations(connection) == []
        assert _applied_at(connection, 6) == at_6
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
        assert {1, 2, 3, 4, 5, 6} <= set(versions)
        assert 900 in versions and 901 not in versions
        assert _TASK_TABLES <= _table_names(connection)
        assert "ROLLBACK_CHECK" not in _table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
    finally:
        connection.close()


# --- task row: FR-TSK-01/02 dimensions --------------------------------------


def test_minimum_task_record_with_defaults(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(conn, proposal_id=proposal_id)
    row = _row(conn, "task", task_id)
    assert row["proposal_id"] == proposal_id
    assert row["approval_state"] == "NOT_REQUIRED"
    assert row["cancellation"] == "ACTIVE"
    assert row["version"] == 1
    assert row["created_at"] == _CREATED_AT


@pytest.mark.parametrize(
    ("task_type", "responsible_vertical_id"),
    [("INTERNAL", None), ("INTERNAL", 1), ("CROSS_VERTICAL", 2)],
)
def test_task_type_consistent_with_verticals(
    conn: sqlite3.Connection,
    task_type: str,
    responsible_vertical_id: int | None,
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(
        conn,
        proposal_id=proposal_id,
        task_type=task_type,
        responsible_vertical_id=responsible_vertical_id,
    )
    row = _row(conn, "task", task_id)
    assert row["task_type"] == task_type


@pytest.mark.parametrize(
    ("task_type", "responsible_vertical_id"),
    [("INTERNAL", 2), ("CROSS_VERTICAL", None), ("CROSS_VERTICAL", 1)],
)
def test_task_type_inconsistent_with_verticals_rejected(
    conn: sqlite3.Connection,
    task_type: str,
    responsible_vertical_id: int | None,
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _task(
            conn,
            proposal_id=proposal_id,
            task_type=task_type,
            responsible_vertical_id=responsible_vertical_id,
        )


def test_task_type_unknown_token_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    for bad in ("ANNOUNCEMENT", " TASK ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _task(conn, proposal_id=proposal_id, task_type=bad)


def test_nonexistent_verticals_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _task(conn, proposal_id=proposal_id, source_vertical_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _task(
            conn,
            proposal_id=proposal_id,
            responsible_vertical_id=999,
            task_type="CROSS_VERTICAL",
        )


@pytest.mark.parametrize("assignment_mode", ["EXPLICIT", "OPEN"])
def test_assignment_mode_tokens_recordable(
    conn: sqlite3.Connection, assignment_mode: str
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(conn, proposal_id=proposal_id, assignment_mode=assignment_mode)
    assert _row(conn, "task", task_id)["assignment_mode"] == assignment_mode


def test_assignment_mode_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    for bad in ("NONE", " EXPLICIT ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _task(conn, proposal_id=proposal_id, assignment_mode=bad)


@pytest.mark.parametrize(
    ("assignment_mode", "people_needed", "people_needed_unlimited"),
    [
        ("EXPLICIT", 1, 0),
        ("OPEN", 1, 0),
        ("OPEN", 3, 0),
        ("OPEN", None, 1),
    ],
)
def test_people_needed_encoding_recordable(
    conn: sqlite3.Connection,
    assignment_mode: str,
    people_needed: int,
    people_needed_unlimited: int,
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(
        conn,
        proposal_id=proposal_id,
        assignment_mode=assignment_mode,
        people_needed=people_needed,
        people_needed_unlimited=people_needed_unlimited,
    )
    row = _row(conn, "task", task_id)
    assert row["people_needed"] == people_needed
    assert row["people_needed_unlimited"] == people_needed_unlimited


@pytest.mark.parametrize(
    ("assignment_mode", "people_needed", "people_needed_unlimited"),
    [
        ("EXPLICIT", 3, 0),
        ("EXPLICIT", None, 1),
        ("EXPLICIT", 1, 1),
        ("OPEN", None, 0),
        ("OPEN", 0, 0),
        ("OPEN", 1, 1),
        ("OPEN", 2, 1),
        ("OPEN", -1, 0),
    ],
)
def test_people_needed_encoding_invalid_rejected(
    conn: sqlite3.Connection,
    assignment_mode: str,
    people_needed: int,
    people_needed_unlimited: int,
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _task(
            conn,
            proposal_id=proposal_id,
            assignment_mode=assignment_mode,
            people_needed=people_needed,
            people_needed_unlimited=people_needed_unlimited,
        )


@pytest.mark.parametrize("approval_state", ["NOT_REQUIRED", "PENDING", "APPROVED", "DENIED"])
def test_approval_state_tokens_recordable(
    conn: sqlite3.Connection, approval_state: str
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(conn, proposal_id=proposal_id, approval_state=approval_state)
    assert _row(conn, "task", task_id)["approval_state"] == approval_state


def test_approval_state_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    for bad in ("WAITING", " PENDING ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _task(conn, proposal_id=proposal_id, approval_state=bad)


@pytest.mark.parametrize("cancellation", ["ACTIVE", "CANCELLED"])
def test_cancellation_tokens_recordable(conn: sqlite3.Connection, cancellation: str) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(conn, proposal_id=proposal_id, cancellation=cancellation)
    assert _row(conn, "task", task_id)["cancellation"] == cancellation


def test_cancellation_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    for bad in ("CLOSED", " ACTIVE ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _task(conn, proposal_id=proposal_id, cancellation=bad)


def test_task_version_domain(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    task_id = _task(conn, proposal_id=proposal_id, version=2)
    assert _row(conn, "task", task_id)["version"] == 2
    for bad in (0, -1):
        attempt_id = _attempt(conn)
        next_proposal = _proposal(conn, attempt_id=attempt_id)
        with pytest.raises(sqlite3.IntegrityError):
            _task(conn, proposal_id=next_proposal, version=bad)


def test_task_text_fields_verbatim_and_non_blank(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _task(conn, proposal_id=proposal_id, description="   ")
    with pytest.raises(sqlite3.IntegrityError):
        _task(conn, proposal_id=proposal_id, created_at=" ")
    unicode_text = "clip the wires — two volunteers needed"
    task_id = _task(
        conn,
        proposal_id=proposal_id,
        description=unicode_text,
        created_at=_CREATED_AT,
    )
    assert _row(conn, "task", task_id)["description"] == unicode_text


def test_cross_vertical_task_type_covers_any_responsible_vertical(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-01: CROSS_VERTICAL whenever responsible differs from source."""
    _seed_generated_bucket(conn)
    first = _proposal(conn)
    first_task = _task(
        conn,
        proposal_id=first,
        task_type="CROSS_VERTICAL",
        source_vertical_id=1,
        responsible_vertical_id=2,
        assignment_mode="EXPLICIT",
    )
    assert _row(conn, "task", first_task)["task_type"] == "CROSS_VERTICAL"


def test_each_accepted_proposal_gets_at_most_one_task(
    tmp_path: Path,
) -> None:
    """Schema side of the FR-REV-05/AC-18 single-object rule: one task row
    per proposal; a second accepted-object row is rejected."""
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        _seed_generated_bucket(connection)
        proposal_id = _proposal(connection)
        _task(connection, proposal_id=proposal_id)
        second = connect(_database_file(connection))
        try:
            with transaction(second):
                with pytest.raises(sqlite3.IntegrityError):
                    _task(second, proposal_id=proposal_id)
            count = connection.execute("SELECT COUNT(*) FROM task").fetchone()[0]
            assert count == 1
        finally:
            second.close()
    finally:
        connection.close()


def test_two_tasks_from_two_proposals_coexist(conn: sqlite3.Connection) -> None:
    """AC-02 at task level: independent proposals become independent tasks."""
    _seed_generated_bucket(conn)
    first_attempt = _attempt(conn)
    first = _proposal(conn, attempt_id=first_attempt)
    second = _proposal(conn, primary_message_id=2)
    second_task = _task(conn, proposal_id=second)
    assert first != second
    assert _row(conn, "task", second_task)["proposal_id"] == second


# --- assignee records: FR-TSK-03/05/06, §4.1 --------------------------------


def test_two_assignees_on_one_task(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2)
    _assignee(conn, person_id=3)
    count = conn.execute("SELECT COUNT(*) FROM task_assignee").fetchone()[0]
    assert count == 2


def test_assignee_defaults(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2)
    row = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert row["agreement"] == "AWAITING"
    assert row["work_status"] == "NOT_STARTED"
    assert row["cancelled"] == 0


def test_duplicate_assignee_record_rejected(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=2)


def test_assignee_unknown_task_or_person_rejected(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, task_id=999, person_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=999)


@pytest.mark.parametrize("agreement", ["AWAITING", "ACCEPTED", "DENIED"])
def test_agreement_tokens_recordable(conn: sqlite3.Connection, agreement: str) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(
        conn,
        person_id=2,
        agreement=agreement,
        agreed_at=None if agreement == "AWAITING" else _CREATED_AT,
    )
    row = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert row["agreement"] == agreement


def test_agreement_invalid_rejected(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    for bad in ("ACCEPTED_LATER", " AWAITING ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _assignee(conn, person_id=2, agreement=bad, agreed_at=_CREATED_AT)


@pytest.mark.parametrize("work_status", ["NOT_STARTED", "IN_PROGRESS", "COMPLETED"])
def test_work_status_tokens_recordable(conn: sqlite3.Connection, work_status: str) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2, work_status=work_status)
    row = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert row["work_status"] == work_status


def test_work_status_invalid_rejected(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    for bad in ("DONE", "COMPLETED ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _assignee(conn, person_id=2, work_status=bad)


def test_agreed_at_records_agreement_time(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2, agreement="ACCEPTED", agreed_at="2026-05-01T12:00:00Z")
    row = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert row["agreed_at"] == "2026-05-01T12:00:00Z"


def test_agreed_at_shape_flagged(conn: sqlite3.Connection) -> None:
    """§4.1 assignee timestamps: a decision needs a time; AWAITING has none."""
    _task_from_accepted_proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=2, agreement="ACCEPTED")
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=2, agreement="DENIED", agreed_at=None)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=2, agreement="AWAITING", agreed_at=_CREATED_AT)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=2, agreement="ACCEPTED", agreed_at=" ")


def test_assignee_cancelled_flag_domain(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2, cancelled=1)
    row = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert row["cancelled"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=3, cancelled=2)


def test_task_and_assignee_cancellation_are_separate_dimensions(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-02: cancelling the task must not silently mark every record."""
    _task_from_accepted_proposal(conn, cancellation="CANCELLED")
    _assignee(conn, person_id=2)
    row = conn.execute("SELECT * FROM task").fetchone()
    assert row["cancellation"] == "CANCELLED"
    assignee = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert assignee["cancelled"] == 0


def test_assignee_removal_and_reassignment(conn: sqlite3.Connection) -> None:
    """FR-TSK-05/16: denial frees the place; the creator may reassign."""
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2)
    conn.execute(
        "DELETE FROM task_assignee WHERE task_id = 1 AND person_id = 2"
    )
    remaining = conn.execute("SELECT COUNT(*) FROM task_assignee").fetchone()[0]
    assert remaining == 0
    _assignee(conn, person_id=3)
    replacement = conn.execute("SELECT person_id FROM task_assignee").fetchone()[0]
    assert replacement == 3


def test_assignee_added_at_required(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _assignee(conn, person_id=2, added_at=None)


# --- attributes and affected verticals: FR-TSK-01/FR-PRP-05 -----------------


def test_task_attribute_storage(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    conn.execute(
        "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
        " VALUES (1, 'venue', 'Gate 3')"
    )
    conn.execute(
        "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
        " VALUES (1, 'note', NULL)"
    )
    rows = conn.execute(
        "SELECT attr_key, attr_value FROM task_attribute ORDER BY attr_key"
    ).fetchall()
    assert [(row["attr_key"], row["attr_value"]) for row in rows] == [
        ("note", None),
        ("venue", "Gate 3"),
    ]


def test_task_attribute_unique_per_task_and_unknown_task_rejected(
    conn: sqlite3.Connection,
) -> None:
    _task_from_accepted_proposal(conn)
    conn.execute(
        "INSERT INTO task_attribute (task_id, attr_key, attr_value) VALUES (1, 'venue', 'A')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
            " VALUES (1, 'venue', 'B')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
            " VALUES (999, 'venue', 'A')"
        )


def test_task_affected_vertical_storage(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    conn.execute(
        "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (1, 2)"
    )
    rows = conn.execute("SELECT task_id, vertical_id FROM task_affected_vertical").fetchall()
    assert [(row["task_id"], row["vertical_id"]) for row in rows] == [(1, 2)]


def test_task_affected_vertical_duplicates_and_unknown_rejected(
    conn: sqlite3.Connection,
) -> None:
    _task_from_accepted_proposal(conn)
    conn.execute(
        "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (1, 2)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (1, 2)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (1, 999)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (999, 2)"
        )


# --- history: FR-TSK-11, NFR-AUD-01/03 --------------------------------------


def test_history_row_stored_verbatim(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    history_id = _history(conn)
    row = conn.execute("SELECT * FROM task_history WHERE id = ?", (history_id,)).fetchone()
    assert row["task_id"] == 1
    assert row["field_name"] == "work_status"
    assert row["old_value"] == "NOT_STARTED"
    assert row["new_value"] == "COMPLETED"
    assert row["changed_at"] == "2026-05-01T13:00:00Z"
    assert row["actor_person_id"] == 2
    assert row["actor_role"] == "VOLUNTEER"
    assert row["actor_vertical_id"] == 1
    assert row["reason"] == "performer marked the work completed"


def test_history_is_append_only_like_two_edits(conn: sqlite3.Connection) -> None:
    """FR-TSK-11: a changed value shall not overwrite history."""
    _task_from_accepted_proposal(conn)
    _history(conn, new_value="IN_PROGRESS")
    _history(conn, changed_at="2026-05-01T14:00:00Z")
    rows = conn.execute(
        "SELECT new_value, changed_at FROM task_history ORDER BY changed_at"
    ).fetchall()
    assert [row["new_value"] for row in rows] == ["IN_PROGRESS", "COMPLETED"]
    assert len(rows) == 2


def test_history_actor_is_user_or_model_with_version(
    conn: sqlite3.Connection,
) -> None:
    _task_from_accepted_proposal(conn)
    _history(
        conn,
        actor_person_id=None,
        actor_model_id="model-h",
        actor_model_version="2.0.1",
        actor_role="REVIEWER",
    )
    row = conn.execute("SELECT actor_model_id FROM task_history").fetchone()
    assert row["actor_model_id"] == "model-h"


def test_history_actor_shape_violations_rejected(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _history(conn, actor_person_id=2, actor_model_id="model-h")
    with pytest.raises(sqlite3.IntegrityError):
        _history(
            conn,
            actor_person_id=None,
            actor_model_id=None,
            actor_model_version=None,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _history(
            conn,
            actor_person_id=None,
            actor_model_id="model-h",
            actor_model_version=None,
        )


def test_history_required_fields(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    for updates in (
        {"field_name": None},
        {"field_name": " "},
        {"changed_at": " "},
        {"actor_role": " "},
        {"reason": ""},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _history(conn, **updates)


def test_history_old_value_can_be_empty(conn: sqlite3.Connection) -> None:
    _task_from_accepted_proposal(conn)
    history_id = _history(
        conn, field_name="description", old_value=None, new_value="clip the wires"
    )
    row = conn.execute("SELECT old_value FROM task_history WHERE id = ?", (history_id,)).fetchone()
    assert row["old_value"] is None


def test_history_unknown_task_rejected_and_person_deletion_restricted(
    conn: sqlite3.Connection,
) -> None:
    _task_from_accepted_proposal(conn)
    _history(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _history(conn, task_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = 2")


# --- transitions are recorded, not recomputed: FR-TSK-06/13 -----------------


def test_work_status_and_agreement_changes_are_recorded_in_history(
    conn: sqlite3.Connection,
) -> None:
    """Schema contribution to FR-TSK-06/13: vocabulary CHECKs on the current
    dimension values plus append-only history rows; legality over time is
    service-level (no triggers, no cross-row time machine)."""
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2, agreement="ACCEPTED", agreed_at=_CREATED_AT)
    _history(conn, field_name="agreement", old_value="AWAITING", new_value="ACCEPTED")
    conn.execute(
        "UPDATE task_assignee SET work_status = 'IN_PROGRESS'"
        " WHERE task_id = 1 AND person_id = 2"
    )
    _history(conn, field_name="work_status", old_value="NOT_STARTED", new_value="IN_PROGRESS")
    conn.execute(
        "UPDATE task_assignee SET work_status = 'COMPLETED'"
        " WHERE task_id = 1 AND person_id = 2"
    )
    _history(conn, field_name="work_status", old_value="IN_PROGRESS", new_value="COMPLETED")
    row = conn.execute("SELECT * FROM task_assignee").fetchone()
    assert row["work_status"] == "COMPLETED"
    assert row["agreement"] == "ACCEPTED"
    record_count = conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
    assert record_count == 3


# --- integrity: DR-01/DR-02, NFR-AUD-01 -------------------------------------


def test_task_chain_foreign_key_check_clean(conn: sqlite3.Connection) -> None:
    """NFR-AUD-01/DR-01: task traces back through proposal and attempt."""
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2)
    conn.execute(
        "INSERT INTO task_attribute (task_id, attr_key, attr_value) VALUES (1, 'venue', 'A')"
    )
    conn.execute(
        "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (1, 2)"
    )
    _history(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deletion_restricted_in_every_direction(conn: sqlite3.Connection) -> None:
    """DR-02: deactivating roster objects never deletes or detaches records."""
    _task_from_accepted_proposal(conn)
    _assignee(conn, person_id=2)
    _history(conn)
    conn.execute(
        "INSERT INTO task_attribute (task_id, attr_key, attr_value) VALUES (1, 'venue', 'A')"
    )
    conn.execute(
        "INSERT INTO task_affected_vertical (task_id, vertical_id) VALUES (1, 2)"
    )
    for statement in (
        "DELETE FROM person WHERE id = 1",
        "DELETE FROM vertical WHERE id = 1",
        "DELETE FROM vertical WHERE id = 2",
        "DELETE FROM proposal WHERE id = 1",
        "DELETE FROM processing_attempt WHERE id = 1",
        "DELETE FROM task WHERE id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
    remaining = conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]
    assert remaining == 1


def test_duplicating_assignees_across_connections_keeps_one_row(
    tmp_path: Path,
) -> None:
    """NFR-CON-02 shape: the same assignee record cannot be inserted twice."""
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        _seed_generated_bucket(connection)
        proposal_id = _proposal(connection)
        _task(connection, proposal_id=proposal_id)
        _assignee(connection, person_id=2)
        second = connect(_database_file(connection))
        try:
            with transaction(second):
                with pytest.raises(sqlite3.IntegrityError):
                    _assignee(second, task_id=1, person_id=2)
            count = connection.execute("SELECT COUNT(*) FROM task_assignee").fetchone()[0]
            assert count == 1
        finally:
            second.close()
    finally:
        connection.close()
