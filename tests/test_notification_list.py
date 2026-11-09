"""Self-only in-app notification list and read markers (FR-NOT-03/07, NFR-SEC-03).

The in-app list is the guaranteed fallback channel: a notification whose
delivery is FAILED, EXHAUSTED or has no endpoint must still appear for its
recipient, unread. No caller (including Admin) gets another person's inbox.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    NotificationAuthorizationError,
    NotificationNotFoundError,
    list_notifications,
    mark_read,
)
from tests.notification_fixtures import (
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
    add_proposal(connection, 1, description="clip the wires")
    add_task(connection, 1, proposal_id=1, description="clip the wires")
    add_assignee(connection, task_id=1, person_id=3, agreement="AWAITING")
    yield connection
    connection.close()


def _notification(
    connection: sqlite3.Connection,
    *,
    recipient_person_id: int,
    event_type: str = "TASK_ASSIGNED",
    created_at: str = "2026-11-01T10:00:00+00:00",
) -> int:
    return add_notification(
        connection,
        event_type=event_type,
        recipient_person_id=recipient_person_id,
        dedupe_key=f"{event_type}:1:{recipient_person_id}:{created_at}",
        created_at=created_at,
        task_id=1,
    )


def test_list_is_self_only(conn: sqlite3.Connection) -> None:
    mine = _notification(conn, recipient_person_id=3)
    _notification(conn, recipient_person_id=4)

    views = list_notifications(conn, person_id=3)

    assert [view.notification_id for view in views] == [mine]


def test_admin_gets_no_cross_person_inbox(conn: sqlite3.Connection) -> None:
    _notification(conn, recipient_person_id=3)
    admin_view = _notification(conn, recipient_person_id=6)

    views = list_notifications(conn, person_id=6)

    assert [view.notification_id for view in views] == [admin_view]


def test_list_is_ordered_by_created_at_then_id(conn: sqlite3.Connection) -> None:
    later = _notification(
        conn,
        recipient_person_id=3,
        created_at="2026-11-01T10:05:00+00:00",
    )
    earlier = _notification(
        conn,
        recipient_person_id=3,
        created_at="2026-11-01T09:55:00+00:00",
    )

    views = list_notifications(conn, person_id=3)

    assert [view.notification_id for view in views] == [earlier, later]


def test_unread_filter_excludes_read_rows(conn: sqlite3.Connection) -> None:
    read_id = _notification(conn, recipient_person_id=3)
    unread_id = _notification(
        conn,
        recipient_person_id=3,
        created_at="2026-11-01T10:05:00+00:00",
    )
    assert mark_read(conn, read_id, person_id=3) is True

    all_views = list_notifications(conn, person_id=3)
    unread_views = list_notifications(conn, person_id=3, unread_only=True)

    assert [view.notification_id for view in all_views] == [read_id, unread_id]
    assert [view.notification_id for view in unread_views] == [unread_id]


@pytest.mark.parametrize("state", ["FAILED", "EXHAUSTED"])
def test_failed_delivery_still_appears_unread(
    conn: sqlite3.Connection, state: str
) -> None:
    notification_id = _notification(conn, recipient_person_id=3)
    conn.execute(
        "UPDATE notification SET delivery_state = ? WHERE id = ?",
        (state, notification_id),
    )

    views = list_notifications(conn, person_id=3, unread_only=True)

    assert [view.notification_id for view in views] == [notification_id]
    assert views[0].read_at is None


def test_no_endpoint_attempt_still_appears_unread(conn: sqlite3.Connection) -> None:
    notification_id = _notification(conn, recipient_person_id=3)
    conn.execute(
        "UPDATE notification SET delivery_state = 'EXHAUSTED' WHERE id = ?",
        (notification_id,),
    )
    conn.execute(
        "INSERT INTO notification_attempt (notification_id, attempt_number, outcome,"
        " error, recorded_at) VALUES (?, 1, 'NO_ENDPOINT', 'NO_ACTIVE_ENDPOINT', ?)",
        (notification_id, "2026-11-01T10:01:00+00:00"),
    )

    views = list_notifications(conn, person_id=3, unread_only=True)

    assert [view.notification_id for view in views] == [notification_id]
    assert views[0].read_at is None


def test_mark_read_is_idempotent(conn: sqlite3.Connection) -> None:
    notification_id = _notification(conn, recipient_person_id=3)

    assert mark_read(conn, notification_id, person_id=3) is True
    assert mark_read(conn, notification_id, person_id=3) is False
    count = conn.execute(
        "SELECT COUNT(*) FROM notification_read WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()[0]
    assert count == 1


def test_mark_read_never_changes_delivery_state(conn: sqlite3.Connection) -> None:
    notification_id = _notification(conn, recipient_person_id=3)
    conn.execute(
        "UPDATE notification SET delivery_state = 'EXHAUSTED' WHERE id = ?",
        (notification_id,),
    )

    assert mark_read(conn, notification_id, person_id=3) is True

    row = conn.execute(
        "SELECT delivery_state FROM notification WHERE id = ?", (notification_id,)
    ).fetchone()
    assert row is not None
    assert row["delivery_state"] == "EXHAUSTED"


def test_other_persons_notification_is_a_recorded_denial(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn, recipient_person_id=3)

    with pytest.raises(NotificationAuthorizationError) as caught:
        mark_read(conn, notification_id, person_id=4)

    assert caught.value.reason == "NOT_RECIPIENT"
    read_count = conn.execute(
        "SELECT COUNT(*) FROM notification_read WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()[0]
    assert read_count == 0
    denial = conn.execute(
        "SELECT actor_person_id, operation, subject_type, subject_id, reason"
        " FROM authorization_denial"
    ).fetchone()
    assert denial is not None
    assert int(denial["actor_person_id"]) == 4
    assert denial["operation"] == "NOTIFICATION_READ"
    assert denial["subject_type"] == "NOTIFICATION"
    assert int(denial["subject_id"]) == notification_id
    assert denial["reason"] == "NOT_RECIPIENT"


def test_mark_read_unknown_notification_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(NotificationNotFoundError):
        mark_read(conn, 999, person_id=3)
