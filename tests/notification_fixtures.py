"""Shared synthetic rows for Phase 11 notification tests.

Every value here is fake: synthetic names, synthetic phones/roll numbers and
synthetic push tokens. No real credential, phone number or OTP code appears.
"""

from __future__ import annotations

import sqlite3
from typing import Any

CREATED_AT = "2026-11-01T10:00:00+00:00"
GENERATED_AT = "2026-11-01T09:59:00+00:00"
TASK_CREATED_AT = "2026-11-01T09:59:30+00:00"
REQUEST_AT = "2026-11-01T09:59:45+00:00"
DECIDED_AT = "2026-11-01T10:00:00+00:00"


def add_vertical(
    connection: sqlite3.Connection, vertical_id: int, name: str
) -> int:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name)
    )
    return vertical_id


def add_person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    role: str,
    *,
    reports_to: int | None = None,
    active: int = 1,
    vertical_id: int | None = None,
) -> int:
    connection.execute(
        "INSERT INTO person (id, name, phone, role, reports_to, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            name,
            f"ph-{person_id}",
            role,
            reports_to,
            active,
            f"roll-{person_id}",
        ),
    )
    if vertical_id is not None:
        connection.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
            (person_id, vertical_id),
        )
    return person_id


def seed_roster(connection: sqlite3.Connection) -> None:
    """Two verticals and a small reporting tree of active/inactive people."""
    add_vertical(connection, 1, "Logistics")
    add_vertical(connection, 2, "Decor")
    add_person(connection, 1, "Head One", "Head", vertical_id=1)
    add_person(connection, 2, "AH One", "AH", reports_to=1, vertical_id=1)
    add_person(connection, 3, "Volunteer One", "Volunteer", reports_to=2, vertical_id=1)
    add_person(connection, 4, "Volunteer Two", "Volunteer", reports_to=2, vertical_id=1)
    add_person(connection, 5, "Inactive Volunteer", "Volunteer", reports_to=2,
               active=0, vertical_id=1)
    add_person(connection, 6, "Admin One", "Admin", vertical_id=2)
    add_person(connection, 7, "AH Two", "AH", vertical_id=2)
    add_person(connection, 8, "Volunteer Elsewhere", "Volunteer", reports_to=7,
               vertical_id=2)


def seed_message_chain(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (1, 'discord', 'msg-1', 1, 2, '2026-11-01T09:58:00+00:00',"
        " '2026-11-01T09:58:00+00:00', 'private source chat context')"
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


def add_proposal(
    connection: sqlite3.Connection,
    proposal_id: int = 1,
    *,
    attempt_id: int = 1,
    bucket_id: int = 1,
    primary_message_id: int = 1,
    description: str | None = "clip the wires",
    ptype: str = "TASK",
    assignment_mode: str = "EXPLICIT",
    task_type: str = "INTERNAL",
    source_vertical_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "id": proposal_id,
        "attempt_id": attempt_id,
        "bucket_id": bucket_id,
        "primary_message_id": primary_message_id,
        "source_vertical_id": source_vertical_id,
        "ptype": ptype,
        "description": description,
        "assignment_mode": assignment_mode,
        "task_type": task_type,
        "structurally_valid": 1,
        "generated_at": GENERATED_AT,
        "model_id": "model-p",
        "model_version": "1.0.0",
        "prompt_version": "prompt-1",
    }
    record.update(fields)
    columns = list(record)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO proposal ({', '.join(columns)}) VALUES ({placeholders})",
        [record[column] for column in columns],
    )
    return proposal_id


def add_task(
    connection: sqlite3.Connection,
    task_id: int = 1,
    *,
    proposal_id: int = 1,
    description: str = "clip the wires",
    task_type: str = "INTERNAL",
    source_vertical_id: int = 1,
    responsible_vertical_id: int = 1,
    creator_id: int = 2,
    assignment_mode: str = "EXPLICIT",
    cancellation: str = "ACTIVE",
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "id": task_id,
        "proposal_id": proposal_id,
        "task_type": task_type,
        "source_vertical_id": source_vertical_id,
        "responsible_vertical_id": responsible_vertical_id,
        "creator_id": creator_id,
        "description": description,
        "assignment_mode": assignment_mode,
        "people_needed": None,
        "people_needed_unlimited": 0,
        "cancellation": cancellation,
        "created_at": TASK_CREATED_AT,
    }
    record.update(fields)
    columns = list(record)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO task ({', '.join(columns)}) VALUES ({placeholders})",
        [record[column] for column in columns],
    )
    return task_id


def add_assignee(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    person_id: int,
    agreement: str = "AWAITING",
    work_status: str = "NOT_STARTED",
    agreed_at: str | None = None,
    cancelled: int = 0,
    added_at: str = TASK_CREATED_AT,
) -> None:
    connection.execute(
        "INSERT INTO task_assignee (task_id, person_id, agreement, agreed_at,"
        " work_status, cancelled, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, person_id, agreement, agreed_at, work_status, cancelled, added_at),
    )


def add_approval_request(
    connection: sqlite3.Connection,
    request_id: int = 1,
    *,
    task_id: int = 2,
    requested_by: int = 2,
    request_message: str | None = "please approve",
) -> int:
    connection.execute(
        "INSERT INTO approval_request (id, task_id, requested_by, request_message,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, task_id, requested_by, request_message, REQUEST_AT),
    )
    return request_id


def add_approval_decision(
    connection: sqlite3.Connection,
    *,
    request_id: int = 1,
    decided_by: int = 1,
    decision: str = "APPROVED",
    decision_message: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO approval_decision (request_id, decided_by, decision,"
        " decision_message, decided_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, decided_by, decision, decision_message, DECIDED_AT),
    )


def add_announcement(
    connection: sqlite3.Connection,
    announcement_id: int = 1,
    *,
    proposal_id: int = 3,
    accepted_by: int = 1,
    audience_type: str = "VERTICAL",
    audience_vertical_id: int = 1,
    audience_ah_person_id: int | None = None,
) -> int:
    connection.execute(
        "INSERT INTO announcement (id, proposal_id, accepted_by, accepted_at,"
        " audience_type, audience_vertical_id, audience_ah_person_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            announcement_id,
            proposal_id,
            accepted_by,
            CREATED_AT,
            audience_type,
            audience_vertical_id,
            audience_ah_person_id,
        ),
    )
    return announcement_id


def add_notification(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    recipient_person_id: int,
    dedupe_key: str,
    created_at: str = CREATED_AT,
    proposal_id: int | None = None,
    task_id: int | None = None,
    approval_request_id: int | None = None,
    announcement_id: int | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO notification (event_type, recipient_person_id, dedupe_key,"
        " proposal_id, task_id, approval_request_id, announcement_id,"
        " delivery_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
        (
            event_type,
            recipient_person_id,
            dedupe_key,
            proposal_id,
            task_id,
            approval_request_id,
            announcement_id,
            created_at,
        ),
    )
    return int(cursor.lastrowid or 0)
