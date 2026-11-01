"""Schema-level tests for the 0008_notifications migration (Slice 2.9).

Verifies: migration application/idempotency/failure isolation; notification
storage per FR-NOT-01/§4.1 (event, recipient, delivery state) with exactly
one subject — proposal, task, approval request or announcement — connecting
each notification into the existing record chains (DR-01); deduplication via
UNIQUE dedupe_key (FR-NOT-05, NFR-REL-01: a retry must not create a duplicate
notification for the same event and recipient); retry-attempt tracking per
FR-NOT-04/NFR-REL-02/AC-17 (append-only attempt rows with an outcome, error
text and time; a failed attempt is recorded and never undoes the committed
change that raised it); delete restrictions (DR-02); concurrency shape.

Storage-only and deliberately not enforced here: composing/sending
notifications or workers (FR-NOT-01 mapping of rows to event_type values,
FR-NOT-03 wording, FR-NOT-06 push capability and FR-NOT-07 channels are
Phase 3/app surfaces, FR-SEC/NFR-SEC-01 credential handling); delivery-state
vocabulary (the SRS names no token set, so delivery_state/outcome/event_type
are non-blank free text the service manages); automatic retry cadence
(NFR-REL-02); masking on AI inputs (NFR-SEC-02 — notifications are not AI
calls).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_NOTIFICATION_TABLES = {
    "notification",
    "notification_attempt",
}

_CREATED_AT = "2026-05-01T11:20:00Z"
_GENERATED_AT = "2026-05-01T11:05:00Z"
_DECISION_RUN_AT = "2026-05-01T11:00:00Z"
_TASK_CREATED_AT = "2026-05-01T11:10:00Z"
_REQUEST_AT = "2026-05-01T11:15:00Z"
_DECIDED_AT = "2026-05-01T11:45:00Z"


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
    # PB-5: mandatory unique synthetic phone/roll_number for every roster row.
    cursor = connection.execute(
        "INSERT INTO person (name, phone, role, roll_number) VALUES (?, ?, ?, ?)",
        (name, f"ph-{name}", role, f"roll-{name}"),
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


def _seed_roster(connection: sqlite3.Connection) -> None:
    _add_vertical(connection, 1, "logistics")
    _add_vertical(connection, 2, "decor")
    _add_person(connection, "creator", "AH")
    _add_person(connection, "volunteer-a", "Volunteer")
    _add_person(connection, "other-ah", "AH")
    _add_person(connection, "responsible-head", "Head")


def _seed_message_chain(connection: sqlite3.Connection) -> None:
    _seed_roster(connection)
    _add_group(connection)
    _add_message(connection)
    _add_bucket(connection)
    _join_message(connection)


def _seed_all_subjects(connection: sqlite3.Connection) -> None:
    """One chain holding every notification subject type: proposal, task,
    approval request and announcement (all keyed to distinct records)."""
    _seed_message_chain(connection)
    attempt_id = _attempt(connection)
    proposal_id = _proposal(connection, attempt_id=attempt_id, ptype="ANNOUNCEMENT",
                            assignment_mode="NONE")
    _announcement_row(connection, proposal_id=proposal_id)
    second_attempt = _attempt(connection)
    task_proposal = _proposal(
        connection,
        attempt_id=second_attempt,
        bucket_id=1,
        primary_message_id=1,
        ptype="TASK",
        assignment_mode="EXPLICIT",
    )
    _task_row(connection, proposal_id=task_proposal)
    _task_request_row(connection, task_id=_task_id(connection))


def _announcement_row(
    connection: sqlite3.Connection,
    proposal_id: int,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "accepted_by": 1,
        "accepted_at": _CREATED_AT,
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


def _task_row(
    connection: sqlite3.Connection,
    proposal_id: int,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "task_type": "INTERNAL",
        "source_vertical_id": 1,
        # PB-3/FR-TSK-09: INTERNAL responsible is the source vertical, never NULL.
        "responsible_vertical_id": 1,
        "creator_id": 1,
        "description": "clip the wires",
        "assignment_mode": "EXPLICIT",
        # PB-3/FR-TSK-04: EXPLICIT tasks store people_needed NULL.
        "people_needed": None,
        "people_needed_unlimited": 0,
        "created_at": _TASK_CREATED_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO task ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _task_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT id FROM task").fetchone()
    assert row is not None
    return int(row["id"])


def _task_request_row(
    connection: sqlite3.Connection,
    task_id: int,
    **fields: Any,
) -> int:
    """CROSS_VERTICAL request for the approval_request subject (FR-XV-03)."""
    connection.execute(
        "UPDATE task SET task_type = 'CROSS_VERTICAL', responsible_vertical_id = 2"
        " WHERE id = ?",
        (task_id,),
    )
    record: dict[str, Any] = {
        "task_id": task_id,
        "requested_by": 3,
        "created_at": _REQUEST_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO approval_request ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _notification(
    connection: sqlite3.Connection,
    recipient_person_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "event_type": "task_assigned",
        "recipient_person_id": recipient_person_id,
        "dedupe_key": "task-assigned:task-1:recipient-1",
        "created_at": _CREATED_AT,
    }
    if not any(
        name in fields
        for name in ("proposal_id", "task_id",
                     "approval_request_id", "announcement_id")
    ):
        record["task_id"] = _task_id(connection)
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO notification ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _attempt_row(
    connection: sqlite3.Connection,
    notification_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "notification_id": notification_id,
        "attempt_number": 1,
        "outcome": "SUCCEEDED",
        "recorded_at": _CREATED_AT,
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO notification_attempt ({', '.join(record)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


# --- migration application --------------------------------------------------


def test_notification_tables_created_after_earlier_migrations(
    conn: sqlite3.Connection,
) -> None:
    assert {1, 2, 3, 4, 5, 6, 7, 8, 9} <= set(_versions(conn))
    assert _NOTIFICATION_TABLES <= _table_names(conn)


def test_notification_migration_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        at_8 = _applied_at(connection, 8)
        assert apply_migrations(connection) == []
        assert _applied_at(connection, 8) == at_8
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
        assert {1, 2, 3, 4, 5, 6, 7, 8, 9} <= set(versions)
        assert 900 in versions and 901 not in versions
        assert _NOTIFICATION_TABLES <= _table_names(connection)
        assert "ROLLBACK_CHECK" not in _table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM notification").fetchone()[0] == 0
    finally:
        connection.close()


# --- notification rows: FR-NOT-01/§4.1, exactly one subject -----------------


def test_minimum_notification_with_task_subject(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    task_id = _task_row(conn, proposal_id=1)
    notification_id = _notification(conn, task_id=task_id)
    row = _row(conn, "notification", notification_id)
    assert row["event_type"] == "task_assigned"
    assert row["recipient_person_id"] == 1
    assert row["delivery_state"] == "PENDING"
    assert row["created_at"] == _CREATED_AT
    assert row["proposal_id"] is None
    assert row["approval_request_id"] is None
    assert row["announcement_id"] is None


def test_each_subject_type_records_exactly_one_fk(conn: sqlite3.Connection) -> None:
    """§4.1/DR-01: a notification points at exactly one existing record."""
    _seed_all_subjects(conn)
    announcement_id = conn.execute("SELECT id FROM announcement").fetchone()[0]
    request_id = conn.execute("SELECT id FROM approval_request").fetchone()[0]
    task_id = _task_id(conn)
    subjects = [
        {"proposal_id": 2},
        {"task_id": task_id},
        {"approval_request_id": request_id},
        {"announcement_id": announcement_id},
    ]
    for index, subject in enumerate(subjects, start=1):
        notification_id = _notification(
            conn,
            event_type=f"event-{index}",
            dedupe_key=f"event-{index}:recipient-1",
            recipient_person_id=1,
            **subject,
        )
        row = _row(conn, "notification", notification_id)
        non_null = [name for name, _ in subject.items() if row[name] is not None]
        assert non_null == list(subject)
        assert row["event_type"] == f"event-{index}"


@pytest.mark.parametrize(
    ("proposal_id", "task_id", "request_id", "announcement_id"),
    [
        (None, None, None, None),
        (2, 1, None, None),
        (None, 1, 1, None),
        (None, None, 1, 1),
    ],
)
def test_subject_count_violations_rejected(
    conn: sqlite3.Connection,
    proposal_id: int | None,
    task_id: int | None,
    request_id: int | None,
    announcement_id: int | None,
) -> None:
    _seed_all_subjects(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notification (event_type, recipient_person_id, dedupe_key,"
            " proposal_id, task_id, approval_request_id, announcement_id, created_at)"
            " VALUES ('event-x', 1, 'event-x:dup', ?, ?, ?, ?, ?)",
            (proposal_id, task_id, request_id, announcement_id, _CREATED_AT),
        )


def test_unknown_subject_or_recipient_rejected(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    _task_row(conn, proposal_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _notification(conn, recipient_person_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notification (event_type, recipient_person_id, dedupe_key,"
            " task_id, created_at) VALUES ('event-x', 1, 'event-x:miss', 999, ?)",
            (_CREATED_AT,),
        )


def test_required_text_fields(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    proposal_id = _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _notification(conn, event_type=" ", proposal_id=proposal_id,
                      task_id=None, dedupe_key="event-a:1")
    with pytest.raises(sqlite3.IntegrityError):
        _notification(conn, dedupe_key="", proposal_id=proposal_id)
    with pytest.raises(sqlite3.IntegrityError):
        _notification(conn, created_at="   ", proposal_id=proposal_id)


def test_two_events_for_one_proposal_coexist(conn: sqlite3.Connection) -> None:
    """FR-NOT-02 shape: one notification per event per proposal, no batching."""
    _seed_message_chain(conn)
    _attempt(conn)
    proposal_id = _proposal(conn)
    _notification(conn, event_type="proposal_ready", dedupe_key="proposal-ready:1",
                  proposal_id=proposal_id)
    _notification(conn, event_type="proposal_reminder", dedupe_key="proposal-remind:1",
                  proposal_id=proposal_id)
    count = conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0]
    assert count == 2


# --- deduplication: FR-NOT-05, NFR-REL-01 -----------------------------------


def test_retry_does_not_duplicate_a_notification(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    task_id = _task_row(conn, proposal_id=1)
    _notification(conn, dedupe_key="task-assigned:task-1:recipient-1", task_id=task_id)
    with pytest.raises(sqlite3.IntegrityError):
        _notification(conn, dedupe_key="task-assigned:task-1:recipient-1", task_id=task_id)
    count = conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0]
    assert count == 1


def test_same_event_for_two_recipients_or_two_tasks_ok(
    conn: sqlite3.Connection,
) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    _task_row(conn, proposal_id=1)
    second_attempt = _attempt(conn)
    second_proposal = _proposal(conn, attempt_id=second_attempt)
    _task_row(conn, proposal_id=second_proposal)
    task_ids = [row["id"] for row in conn.execute("SELECT id FROM task").fetchall()]
    _notification(conn, dedupe_key="task-assigned:task-1:recipient-1", task_id=task_ids[0])
    _notification(
        conn,
        recipient_person_id=2,
        dedupe_key="task-assigned:task-1:recipient-2",
        task_id=task_ids[0],
    )
    _notification(
        conn,
        recipient_person_id=2,
        dedupe_key="task-assigned:task-2:recipient-2",
        task_id=task_ids[1],
    )
    count = conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0]
    assert count == 3


def test_duplicate_dedupe_key_across_connections(
    tmp_path: Path,
) -> None:
    """NFR-CON-02 shape: a concurrent duplicate retry still adds no row."""
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        _seed_message_chain(connection)
        _attempt(connection)
        _proposal(connection)
        task_id = _task_row(connection, proposal_id=1)
        _notification(connection, dedupe_key="task-assigned:task-1:recipient-1",
                      task_id=task_id)
        second = connect(_database_file(connection))
        try:
            with transaction(second):
                with pytest.raises(sqlite3.IntegrityError):
                    second.execute(
                        "INSERT INTO notification (event_type, recipient_person_id,"
                        " dedupe_key, task_id, created_at)"
                        " VALUES ('task_assigned', 1, 'task-assigned:task-1:recipient-1',"
                        " ?, ?)",
                        (task_id, _CREATED_AT),
                    )
            count = connection.execute("SELECT COUNT(*) FROM notification").fetchone()[0]
            assert count == 1
        finally:
            second.close()
    finally:
        connection.close()


def test_delivery_state_records_service_values(
    conn: sqlite3.Connection,
) -> None:
    """§4.1 'delivery state': the SRS names no token set, so the schema
    stores the service's value verbatim."""
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    task_id = _task_row(conn, proposal_id=1)
    notification_id = _notification(
        conn, delivery_state="SENT", task_id=task_id
    )
    assert _row(conn, "notification", notification_id)["delivery_state"] == "SENT"


# --- attempts: FR-NOT-04, NFR-REL-01/02, AC-17 ------------------------------


def test_attempt_rows_stored_and_ordered(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    task_id = _task_row(conn, proposal_id=1)
    notification_id = _notification(conn, task_id=task_id)
    _attempt_row(conn, notification_id=notification_id, attempt_number=1)
    _attempt_row(
        conn,
        notification_id=notification_id,
        attempt_number=2,
        outcome="FAILED",
        error="push channel unavailable",
        recorded_at="2026-05-01T11:25:00Z",
    )
    rows = conn.execute(
        "SELECT attempt_number, outcome, error, recorded_at FROM notification_attempt"
        " ORDER BY attempt_number"
    ).fetchall()
    assert [row["attempt_number"] for row in rows] == [1, 2]
    assert rows[0]["outcome"] == "SUCCEEDED"
    assert rows[1]["outcome"] == "FAILED"
    assert rows[1]["error"] == "push channel unavailable"


def test_attempt_uniqueness_and_number_domain(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    task_id = _task_row(conn, proposal_id=1)
    notification_id = _notification(conn, task_id=task_id)
    _attempt_row(conn, notification_id=notification_id, attempt_number=1)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_row(conn, notification_id=notification_id, attempt_number=1)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_row(conn, notification_id=notification_id, attempt_number=0)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_row(conn, notification_id=999, attempt_number=1)


def test_attempt_outcome_and_error_domains(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _proposal(conn)
    task_id = _task_row(conn, proposal_id=1)
    notification_id = _notification(conn, task_id=task_id)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_row(conn, notification_id=notification_id, outcome="  ")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_row(conn, notification_id=notification_id, error=" ")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_row(conn, notification_id=notification_id, recorded_at="")


def test_notification_failure_does_not_undo_the_committed_change(
    conn: sqlite3.Connection,
) -> None:
    """AC-17: the task change is committed; the failure is recorded as its
    own row; the task row is untouched by the attempt records."""
    _seed_message_chain(conn)
    _attempt(conn)
    proposal_id = _proposal(conn)
    task_id = _task_row(conn, proposal_id=proposal_id, description="clip the wires")
    notification_id = _notification(conn, event_type="task_assigned", task_id=task_id)
    _attempt_row(conn, notification_id=notification_id, attempt_number=1,
                 outcome="SUCCEEDED")
    task_before = _row(conn, "task", task_id)["description"]
    _attempt_row(
        conn,
        notification_id=notification_id,
        attempt_number=2,
        outcome="FAILED",
        error="push channel unavailable",
        recorded_at="2026-05-01T11:25:00Z",
    )
    assert _row(conn, "task", task_id)["description"] == task_before
    assert _row(conn, "notification", notification_id)["delivery_state"] == "PENDING"
    attempts = conn.execute(
        "SELECT outcome FROM notification_attempt ORDER BY attempt_number"
    ).fetchall()
    assert [row["outcome"] for row in attempts] == ["SUCCEEDED", "FAILED"]


# --- integrity: DR-01/DR-02 -------------------------------------------------


def test_notification_chain_foreign_key_check_clean(conn: sqlite3.Connection) -> None:
    _seed_all_subjects(conn)
    announcement_id = conn.execute("SELECT id FROM announcement").fetchone()[0]
    proposal_id = 2
    _notification(conn, event_type="announcement", dedupe_key="announcement:1",
                  recipient_person_id=2, announcement_id=announcement_id)
    _notification(conn, event_type="proposal_ready", dedupe_key="proposal-ready:2",
                  recipient_person_id=1, proposal_id=proposal_id)
    _attempt_row(conn, notification_id=1, attempt_number=1)
    _attempt_row(conn, notification_id=2, attempt_number=1, outcome="FAILED",
                 error="push channel unavailable")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_subject_deletion_restricted(conn: sqlite3.Connection) -> None:
    _seed_all_subjects(conn)
    task_id = _task_id(conn)
    announcement_id = conn.execute("SELECT id FROM announcement").fetchone()[0]
    _notification(conn, dedupe_key="task-assigned:task-1:recipient-1", task_id=task_id)
    _notification(
        conn,
        event_type="announcement",
        dedupe_key="announcement:audience-1",
        recipient_person_id=2,
        announcement_id=announcement_id,
    )
    _attempt_row(conn)
    conn.execute("INSERT INTO approval_recipient (request_id, person_id) VALUES (1, 3)")
    for statement in (
        "DELETE FROM person WHERE id = 1",
        "DELETE FROM task WHERE id = 1",
        "DELETE FROM proposal WHERE id = 2",
        "DELETE FROM announcement WHERE id = 1",
        "DELETE FROM approval_request WHERE id = 1",
        "DELETE FROM notification WHERE id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
    remaining = conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0]
    assert remaining == 2
