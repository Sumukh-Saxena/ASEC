"""Shared notification staging (Phase 7 S4; FR-NOT-02/FR-NOT-04/FR-NOT-05).

Rows only: this module never delivers anything (Phase 11 owns transport) and
never raises on a duplicate. ``stage_notification`` is the moved
``review._insert_notification`` (plan §4.4, no new semantics): one row per
``dedupe_key``, ``ON CONFLICT DO NOTHING``, returning whether a row was
inserted so a retry can be distinguished from a first attempt (FR-NOT-05,
NFR-REL-01).

The event labels follow ``0008``'s deliberate free-text storage; the six task
lifecycle labels are the vocabulary the Phase 7 task service stages. Their
dedupe keys are ``<event>:<task_id>:<person_id>`` so one recipient receives at
most one notification per task event.
"""

from __future__ import annotations

import sqlite3

# Phase 2/6 event labels (moved from ``review.py``; the values are unchanged).
EVENT_PROPOSAL_READY = "PROPOSAL_READY"
EVENT_TASK_ASSIGNED = "TASK_ASSIGNED"
EVENT_OPEN_TASK_AVAILABLE = "OPEN_TASK_AVAILABLE"
EVENT_CROSS_VERTICAL_REQUEST = "CROSS_VERTICAL_REQUEST"
EVENT_HEAD_CROSS_VERTICAL_TASK = "HEAD_CROSS_VERTICAL_TASK"

# Phase 8 decision label (plan §4.4): the requester hears the decision.
EVENT_CROSS_VERTICAL_DECISION = "CROSS_VERTICAL_DECISION"

# Phase 11 announcement fan-out label (plan §4.5). One row per recipient.
EVENT_ANNOUNCEMENT = "ANNOUNCEMENT"

# Phase 7 task lifecycle labels (plan §4.4). No other vocabulary is invented.
EVENT_ASSIGNEE_ACCEPTED = "ASSIGNEE_ACCEPTED"
EVENT_ASSIGNEE_DENIED = "ASSIGNEE_DENIED"
EVENT_WORK_COMPLETED = "WORK_COMPLETED"
EVENT_TASK_EDITED = "TASK_EDITED"
EVENT_TASK_REASSIGNED = "TASK_REASSIGNED"
EVENT_TASK_CANCELLED = "TASK_CANCELLED"

TASK_LIFECYCLE_EVENTS = frozenset(
    {
        EVENT_ASSIGNEE_ACCEPTED,
        EVENT_ASSIGNEE_DENIED,
        EVENT_WORK_COMPLETED,
        EVENT_TASK_EDITED,
        EVENT_TASK_REASSIGNED,
        EVENT_TASK_CANCELLED,
    }
)


def stage_notification(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    recipient_person_id: int,
    dedupe_key: str,
    created_at: str,
    proposal_id: int | None = None,
    task_id: int | None = None,
    approval_request_id: int | None = None,
    announcement_id: int | None = None,
) -> bool:
    """Stage one notification row, returning whether it was newly inserted."""
    cursor = connection.execute(
        "INSERT INTO notification (event_type, recipient_person_id, dedupe_key,"
        " proposal_id, task_id, approval_request_id, announcement_id,"
        " delivery_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)"
        " ON CONFLICT (dedupe_key) DO NOTHING",
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
    return cursor.rowcount == 1


def proposal_ready_key(proposal_id: int) -> str:
    """Dedupe key for the reviewer notification of one proposal (FR-PRP-17)."""
    return f"{EVENT_PROPOSAL_READY}:{proposal_id}"


def task_assignee_key(event_type: str, task_id: int, person_id: int) -> str:
    """Dedupe key for one task event delivered to one person."""
    return f"{event_type}:{task_id}:{person_id}"


def approval_request_key(request_id: int, person_id: int) -> str:
    """Dedupe key for one cross-vertical approval request and recipient."""
    return f"{EVENT_CROSS_VERTICAL_REQUEST}:{request_id}:{person_id}"


def cross_vertical_decision_key(request_id: int, person_id: int) -> str:
    """Dedupe key for one approval decision delivered to one person."""
    return f"{EVENT_CROSS_VERTICAL_DECISION}:{request_id}:{person_id}"


def announcement_key(announcement_id: int, person_id: int) -> str:
    """Dedupe key for one announcement delivered to one recipient (plan §4.5)."""
    return f"{EVENT_ANNOUNCEMENT}:{announcement_id}:{person_id}"
