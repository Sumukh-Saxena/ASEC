"""State-accurate notification rendering (FR-NOT-01/03/07, FR-REV-10, NFR-SEC-03).

The body is composed from the current domain state at read time, never from a
frozen sentence. These tests pin the wording that must not drift: an awaiting
assignee is never called "accepted", an acceptance is never called "assigned",
a cancellation says cancelled, an open place is not an assignment, and an
announcement body is the accepted description only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    NotificationRenderError,
    mark_read,
    render_notification,
)
from tests.notification_fixtures import (
    add_announcement,
    add_approval_decision,
    add_approval_request,
    add_assignee,
    add_notification,
    add_proposal,
    add_task,
    seed_message_chain,
    seed_roster,
)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    seed_roster(connection)
    seed_message_chain(connection)
    yield connection
    connection.close()


def _task_notification(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    task_id: int = 1,
    recipient_person_id: int = 3,
) -> int:
    return add_notification(
        connection,
        event_type=event_type,
        recipient_person_id=recipient_person_id,
        dedupe_key=f"{event_type}:{task_id}:{recipient_person_id}",
        task_id=task_id,
    )


def _seed_task_one(connection: sqlite3.Connection) -> None:
    add_proposal(connection, 1, description="clip the wires")
    add_task(connection, 1, proposal_id=1, description="clip the wires")


def _seed_cross_vertical(connection: sqlite3.Connection) -> None:
    add_proposal(
        connection,
        2,
        attempt_id=1,
        description="carry the ladder",
        task_type="CROSS_VERTICAL",
    )
    add_task(
        connection,
        2,
        proposal_id=2,
        description="carry the ladder",
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
    )
    add_approval_request(connection, 1, task_id=2, request_message="Need two ladders")


def test_proposal_ready_renders_summary_and_review_deep_link(
    conn: sqlite3.Connection,
) -> None:
    add_proposal(conn, 1, description="Stage the lights")
    notification_id = add_notification(
        conn,
        event_type="PROPOSAL_READY",
        recipient_person_id=1,
        dedupe_key="PROPOSAL_READY:1",
        proposal_id=1,
    )

    view = render_notification(conn, notification_id)

    assert view.notification_id == notification_id
    assert view.event_type == "PROPOSAL_READY"
    assert "Proposal" in view.title
    assert "Stage the lights" in view.body
    assert view.deep_link == "/review/1"
    assert view.created_at == "2026-11-01T10:00:00+00:00"
    assert view.read_at is None


def test_awaiting_assignee_is_never_rendered_as_accepted(
    conn: sqlite3.Connection,
) -> None:
    _seed_task_one(conn)
    add_assignee(conn, task_id=1, person_id=3, agreement="AWAITING")
    notification_id = _task_notification(conn, event_type="TASK_ASSIGNED")

    body = render_notification(conn, notification_id).body

    assert "awaiting your response" in body
    assert "accepted" not in body.lower()


def test_task_assigned_after_acceptance_is_state_accurate(
    conn: sqlite3.Connection,
) -> None:
    _seed_task_one(conn)
    add_assignee(
        conn,
        task_id=1,
        person_id=3,
        agreement="ACCEPTED",
        agreed_at="2026-11-01T10:01:00+00:00",
    )
    notification_id = _task_notification(conn, event_type="TASK_ASSIGNED")

    body = render_notification(conn, notification_id).body

    assert "accepted" in body.lower()
    assert "awaiting your response" not in body


def test_acceptance_is_never_rendered_as_assignment(
    conn: sqlite3.Connection,
) -> None:
    _seed_task_one(conn)
    add_assignee(
        conn,
        task_id=1,
        person_id=3,
        agreement="ACCEPTED",
        agreed_at="2026-11-01T10:01:00+00:00",
    )
    notification_id = _task_notification(conn, event_type="ASSIGNEE_ACCEPTED")

    body = render_notification(conn, notification_id).body

    assert "Volunteer One" in body
    assert "accepted" in body.lower()
    assert "assigned to you" not in body.lower()


def test_acceptance_notification_names_the_assignee_not_the_recipient(
    conn: sqlite3.Connection,
) -> None:
    _seed_task_one(conn)
    add_assignee(
        conn,
        task_id=1,
        person_id=3,
        agreement="ACCEPTED",
        agreed_at="2026-11-01T10:01:00+00:00",
    )
    notification_id = _task_notification(
        conn, event_type="ASSIGNEE_ACCEPTED", recipient_person_id=2
    )

    body = render_notification(conn, notification_id).body

    assert "Volunteer One" in body
    assert "AH One" not in body


def test_denial_renders_denied(conn: sqlite3.Connection) -> None:
    _seed_task_one(conn)
    add_assignee(
        conn,
        task_id=1,
        person_id=3,
        agreement="DENIED",
        agreed_at="2026-11-01T10:01:00+00:00",
    )
    notification_id = _task_notification(conn, event_type="ASSIGNEE_DENIED")

    body = render_notification(conn, notification_id).body

    assert "denied" in body.lower()


def test_work_completed_and_edited_and_reassigned_render_their_state(
    conn: sqlite3.Connection,
) -> None:
    _seed_task_one(conn)
    add_assignee(conn, task_id=1, person_id=3, agreement="AWAITING")
    completed = _task_notification(conn, event_type="WORK_COMPLETED")
    edited = _task_notification(conn, event_type="TASK_EDITED")
    reassigned = _task_notification(conn, event_type="TASK_REASSIGNED")

    assert "completed" in render_notification(conn, completed).body.lower()
    assert "updated" in render_notification(conn, edited).body.lower()
    reassigned_body = render_notification(conn, reassigned).body.lower()
    assert "reassigned" in reassigned_body
    assert "awaiting your response" in reassigned_body


def test_cancelled_task_says_cancelled(conn: sqlite3.Connection) -> None:
    _seed_task_one(conn)
    conn.execute("UPDATE task SET cancellation = 'CANCELLED' WHERE id = 1")
    notification_id = _task_notification(conn, event_type="TASK_CANCELLED")

    body = render_notification(conn, notification_id).body

    assert "cancelled" in body.lower()


def test_open_place_is_not_rendered_as_an_assignment(
    conn: sqlite3.Connection,
) -> None:
    add_proposal(conn, 1, assignment_mode="OPEN", description="set up chairs")
    add_task(
        conn,
        1,
        proposal_id=1,
        description="set up chairs",
        assignment_mode="OPEN",
        people_needed=3,
    )
    notification_id = _task_notification(conn, event_type="OPEN_TASK_AVAILABLE")

    body = render_notification(conn, notification_id).body

    assert "open" in body.lower()
    assert "assigned to you" not in body.lower()


def test_cross_vertical_request_renders_the_request_message(
    conn: sqlite3.Connection,
) -> None:
    _seed_cross_vertical(conn)
    notification_id = add_notification(
        conn,
        event_type="CROSS_VERTICAL_REQUEST",
        recipient_person_id=1,
        dedupe_key="CROSS_VERTICAL_REQUEST:1:1",
        approval_request_id=1,
    )

    body = render_notification(conn, notification_id).body

    assert "Need two ladders" in body


def test_head_cross_vertical_task_is_awaiting(
    conn: sqlite3.Connection,
) -> None:
    _seed_cross_vertical(conn)
    notification_id = _task_notification(
        conn, event_type="HEAD_CROSS_VERTICAL_TASK", task_id=2, recipient_person_id=1
    )

    body = render_notification(conn, notification_id).body

    assert "cross-vertical" in body.lower()
    assert "awaiting your response" in body.lower()


def test_cross_vertical_decision_includes_the_decision_message(
    conn: sqlite3.Connection,
) -> None:
    _seed_cross_vertical(conn)
    add_approval_decision(
        conn,
        request_id=1,
        decision="APPROVED",
        decision_message="Bring the blue ladder",
    )
    notification_id = add_notification(
        conn,
        event_type="CROSS_VERTICAL_DECISION",
        recipient_person_id=2,
        dedupe_key="CROSS_VERTICAL_DECISION:1:2",
        approval_request_id=1,
    )

    body = render_notification(conn, notification_id).body

    assert "approved" in body.lower()
    assert "Bring the blue ladder" in body


def test_cross_vertical_denial_without_message_still_renders(
    conn: sqlite3.Connection,
) -> None:
    _seed_cross_vertical(conn)
    add_approval_decision(conn, request_id=1, decision="DENIED", decision_message=None)
    notification_id = add_notification(
        conn,
        event_type="CROSS_VERTICAL_DECISION",
        recipient_person_id=2,
        dedupe_key="CROSS_VERTICAL_DECISION:1:2",
        approval_request_id=1,
    )

    body = render_notification(conn, notification_id).body

    assert "denied" in body.lower()


def test_announcement_body_is_the_accepted_description_only(
    conn: sqlite3.Connection,
) -> None:
    add_proposal(
        conn,
        3,
        description="Bring extension cords to Hall B",
        ptype="ANNOUNCEMENT",
        assignment_mode="NONE",
    )
    add_announcement(conn, 1, proposal_id=3, audience_type="VERTICAL")
    notification_id = add_notification(
        conn,
        event_type="ANNOUNCEMENT",
        recipient_person_id=3,
        dedupe_key="ANNOUNCEMENT:1:3",
        announcement_id=1,
    )

    view = render_notification(conn, notification_id)

    assert view.body == "Bring extension cords to Hall B"
    assert "private source chat context" not in view.body
    assert "bucket" not in view.body.lower()


def test_read_state_is_exposed_without_changing_delivery_state(
    conn: sqlite3.Connection,
) -> None:
    _seed_task_one(conn)
    notification_id = _task_notification(conn, event_type="TASK_ASSIGNED")
    before = conn.execute(
        "SELECT delivery_state FROM notification WHERE id = ?", (notification_id,)
    ).fetchone()
    assert before is not None
    assert mark_read(conn, notification_id, person_id=3) is True

    view = render_notification(conn, notification_id)
    after = conn.execute(
        "SELECT delivery_state FROM notification WHERE id = ?", (notification_id,)
    ).fetchone()

    assert view.read_at is not None
    assert after is not None
    assert after["delivery_state"] == before["delivery_state"]


def test_missing_subject_row_raises_instead_of_half_truth(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        cursor = conn.execute(
            "INSERT INTO notification (event_type, recipient_person_id, dedupe_key,"
            " task_id, delivery_state, created_at) VALUES"
            " ('TASK_ASSIGNED', 3, 'TASK_ASSIGNED:999:3', 999, 'PENDING', ?)",
            ("2026-11-01T10:00:00+00:00",),
        )
        notification_id = int(cursor.lastrowid or 0)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(NotificationRenderError):
        render_notification(conn, notification_id)


def test_unknown_event_type_raises_instead_of_guessing(
    conn: sqlite3.Connection,
) -> None:
    add_proposal(conn, 1)
    notification_id = add_notification(
        conn,
        event_type="MYSTERY_EVENT",
        recipient_person_id=3,
        dedupe_key="MYSTERY_EVENT:1",
        proposal_id=1,
    )

    with pytest.raises(NotificationRenderError):
        render_notification(conn, notification_id)


def test_otp_code_has_no_in_app_renderer(conn: sqlite3.Connection) -> None:
    """The OTP goes only to the OtpSender, never into an in-app body."""
    add_proposal(conn, 1)
    notification_id = add_notification(
        conn,
        event_type="OTP",
        recipient_person_id=3,
        dedupe_key="OTP:FAKE-OTP-CODE-123456",
        proposal_id=1,
    )

    with pytest.raises(NotificationRenderError):
        render_notification(conn, notification_id)
