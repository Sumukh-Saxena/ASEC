"""Shared notification staging tests (Phase 7 S4; FR-NOT-02/FR-NOT-05).

``backend/notification_events.py`` holds the moved ``_insert_notification``
(stage only, no delivery) plus the dedupe-key builders. These tests pin:

* one row per event and recipient, so a retry inserts nothing (FR-NOT-05,
  NFR-REL-01) and the staged row starts ``PENDING`` (FR-NOT-04);
* the key formats the task lifecycle depends on, including the six new task
  event labels (plan §4.4);
* ``review._insert_notification`` stays the same callable and the Phase 6
  ``PROPOSAL_READY`` key does not move.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from backend import review
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_events import (
    EVENT_ASSIGNEE_ACCEPTED,
    EVENT_ASSIGNEE_DENIED,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_EDITED,
    EVENT_TASK_REASSIGNED,
    EVENT_WORK_COMPLETED,
    TASK_LIFECYCLE_EVENTS,
    approval_request_key,
    proposal_ready_key,
    stage_notification,
    task_assignee_key,
)

AH = 1
VOLUNTEER = 2
_CREATED_AT = "2026-11-01T10:00:00+00:00"
_GENERATED_AT = "2026-11-01T09:59:00+00:00"


def _seed(connection: sqlite3.Connection) -> tuple[int, int]:
    """Minimal chain: vertical, roster, bucket, proposal and one task."""
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'PR')")
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, 'Aarav', '+919822220001', 'AH', 1, 'ROLL-001')",
        (AH,),
    )
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, 1)", (AH,)
    )
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, 'Neha', '+919822220002', 'Volunteer', 1, 'ROLL-002')",
        (VOLUNTEER,),
    )
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, 1)",
        (VOLUNTEER,),
    )
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (1, 'discord', 'msg-1', 1, ?, '2026-11-01T09:58:00+00:00',"
        " '2026-11-01T09:58:00+00:00', 'primary line')",
        (AH,),
    )
    connection.execute(
        "INSERT INTO bucket (id, group_id, start_event_timestamp, end_event_timestamp)"
        " VALUES (1, 1, '2026-11-01T09:58:00+00:00', '2026-11-01T09:58:00+00:00')"
    )
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (1, 1, 1)"
    )
    connection.execute(
        "INSERT INTO processing_attempt (id, stage, bucket_id, started_at, outcome)"
        " VALUES (1, 'proposal_generation', 1, '2026-11-01T09:59:00+00:00',"
        " 'SUCCEEDED')"
    )
    connection.execute(
        "INSERT INTO proposal (id, attempt_id, bucket_id, primary_message_id,"
        " source_vertical_id, ptype, assignment_mode, task_type,"
        " structurally_valid, generated_at, model_id, model_version,"
        " prompt_version)"
        " VALUES (1, 1, 1, 1, 1, 'TASK', 'EXPLICIT', 'INTERNAL', 1, ?,"
        " 'model-p', '1.0.0', 'prompt-1')",
        (_GENERATED_AT,),
    )
    connection.execute(
        "INSERT INTO task (id, proposal_id, task_type, source_vertical_id,"
        " responsible_vertical_id, creator_id, description, assignment_mode,"
        " people_needed, people_needed_unlimited, created_at)"
        " VALUES (1, 1, 'INTERNAL', 1, 1, ?, 'clip the wires', 'EXPLICIT', NULL,"
        " 0, '2026-11-01T09:59:30+00:00')",
        (AH,),
    )
    return 1, 1


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    apply_migrations(connection)
    _seed(connection)
    yield connection
    connection.close()


def _notifications(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM notification ORDER BY id").fetchall()


def test_stage_notification_inserts_one_pending_row(
    conn: sqlite3.Connection,
) -> None:
    """FR-NOT-02/FR-NOT-04: staging records the event, not a delivery."""
    inserted = stage_notification(
        conn,
        event_type=EVENT_TASK_ASSIGNED,
        recipient_person_id=VOLUNTEER,
        dedupe_key=task_assignee_key(EVENT_TASK_ASSIGNED, 1, VOLUNTEER),
        created_at=_CREATED_AT,
        task_id=1,
    )

    assert inserted is True
    (row,) = _notifications(conn)
    assert str(row["event_type"]) == EVENT_TASK_ASSIGNED
    assert int(row["recipient_person_id"]) == VOLUNTEER
    assert int(row["task_id"]) == 1
    assert row["proposal_id"] is None
    assert str(row["delivery_state"]) == "PENDING"
    assert str(row["created_at"]) == _CREATED_AT


def test_stage_notification_is_idempotent_per_dedupe_key(
    conn: sqlite3.Connection,
) -> None:
    """FR-NOT-05 / NFR-REL-01: a retry does not duplicate (event, recipient)."""
    key = task_assignee_key(EVENT_TASK_ASSIGNED, 1, VOLUNTEER)
    first = stage_notification(
        conn,
        event_type=EVENT_TASK_ASSIGNED,
        recipient_person_id=VOLUNTEER,
        dedupe_key=key,
        created_at=_CREATED_AT,
        task_id=1,
    )
    second = stage_notification(
        conn,
        event_type=EVENT_TASK_ASSIGNED,
        recipient_person_id=VOLUNTEER,
        dedupe_key=key,
        created_at="2026-11-01T10:05:00+00:00",
        task_id=1,
    )

    assert first is True
    assert second is False
    assert len(_notifications(conn)) == 1


def test_proposal_ready_key_keeps_the_phase_six_format(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-17: the proposal dedupe key is unchanged."""
    assert proposal_ready_key(7) == "PROPOSAL_READY:7"

    inserted = stage_notification(
        conn,
        event_type="PROPOSAL_READY",
        recipient_person_id=AH,
        dedupe_key=proposal_ready_key(1),
        created_at=_CREATED_AT,
        proposal_id=1,
    )

    assert inserted is True
    (row,) = _notifications(conn)
    assert str(row["dedupe_key"]) == "PROPOSAL_READY:1"


def test_task_assignee_key_format() -> None:
    """Plan §4.4: task events dedupe on ``<event>:<task_id>:<person_id>``."""
    assert task_assignee_key("TASK_CANCELLED", 3, 9) == "TASK_CANCELLED:3:9"
    assert task_assignee_key(EVENT_WORK_COMPLETED, 12, 4) == "WORK_COMPLETED:12:4"


def test_approval_request_key_format() -> None:
    """Phase 6 cross-vertical requests dedupe on request and recipient."""
    assert (
        approval_request_key(4, 10) == "CROSS_VERTICAL_REQUEST:4:10"
    )


def test_task_lifecycle_event_labels_are_the_planned_six() -> None:
    """Plan §4.4: the six new task event labels, no extra vocabulary."""
    assert TASK_LIFECYCLE_EVENTS == frozenset(
        {
            "ASSIGNEE_ACCEPTED",
            "ASSIGNEE_DENIED",
            "WORK_COMPLETED",
            "TASK_EDITED",
            "TASK_REASSIGNED",
            "TASK_CANCELLED",
        }
    )
    assert EVENT_ASSIGNEE_ACCEPTED == "ASSIGNEE_ACCEPTED"
    assert EVENT_ASSIGNEE_DENIED == "ASSIGNEE_DENIED"
    assert EVENT_WORK_COMPLETED == "WORK_COMPLETED"
    assert EVENT_TASK_EDITED == "TASK_EDITED"
    assert EVENT_TASK_REASSIGNED == "TASK_REASSIGNED"
    assert EVENT_TASK_CANCELLED == "TASK_CANCELLED"


def test_each_task_lifecycle_event_stages_against_a_task(
    conn: sqlite3.Connection,
) -> None:
    """FR-NOT-01: every lifecycle event is recordable for one task/recipient."""
    for event_type in sorted(TASK_LIFECYCLE_EVENTS):
        assert (
            stage_notification(
                conn,
                event_type=event_type,
                recipient_person_id=VOLUNTEER,
                dedupe_key=task_assignee_key(event_type, 1, VOLUNTEER),
                created_at=_CREATED_AT,
                task_id=1,
            )
            is True
        )

    assert len(_notifications(conn)) == len(TASK_LIFECYCLE_EVENTS)


def test_review_module_keeps_the_shared_staging_alias() -> None:
    """Plan §4.4: ``review._insert_notification`` stays the same callable."""
    assert review._insert_notification is stage_notification


def test_review_proposal_ready_still_uses_the_shared_helper(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-17/FR-NOT-05: the Phase 6 notification path is unchanged."""
    assert review.notify_proposal_ready(conn, 1) == 1
    assert review.notify_proposal_ready(conn, 1) is None

    (row,) = _notifications(conn)
    assert str(row["event_type"]) == "PROPOSAL_READY"
    assert str(row["dedupe_key"]) == "PROPOSAL_READY:1"
    assert int(row["recipient_person_id"]) == AH
