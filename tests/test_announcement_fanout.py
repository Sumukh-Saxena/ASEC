"""Announcement fan-out from the 0018 structured audience (FR-NOT-06, FR-REV-10).

One notification per recipient, dedupe ``ANNOUNCEMENT:<id>:<person>``, an
idempotent sweep that recovers a crash between accept and fan-out, no fallback
to the whole vertical when an AH has no volunteers, and a body that is the
accepted description only (never source chat or bucket context).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    AnnouncementNotFoundError,
    fan_out_announcement,
    render_notification,
    sweep_unfanned_announcements,
)
from backend.notification_events import EVENT_ANNOUNCEMENT, announcement_key
from tests.notification_fixtures import (
    add_announcement,
    add_person,
    add_proposal,
    seed_message_chain,
    seed_roster,
)

_DESCRIPTION = "Bring extension cords to Hall B"
_T0 = "2026-11-01T10:00:00+00:00"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    seed_roster(connection)
    seed_message_chain(connection)
    add_proposal(
        connection,
        3,
        description=_DESCRIPTION,
        ptype="ANNOUNCEMENT",
        assignment_mode="NONE",
    )
    yield connection
    connection.close()


def _vertical_announcement(connection: sqlite3.Connection) -> int:
    return add_announcement(
        connection,
        1,
        proposal_id=3,
        audience_type="VERTICAL",
        audience_vertical_id=1,
        audience_ah_person_id=None,
    )


def _ah_announcement(connection: sqlite3.Connection, ah_person_id: int = 2) -> int:
    return add_announcement(
        connection,
        1,
        proposal_id=3,
        audience_type="AH_VOLUNTEERS",
        audience_vertical_id=1,
        audience_ah_person_id=ah_person_id,
    )


def _notifications(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM notification ORDER BY id").fetchall()


def test_vertical_audience_is_every_active_member(
    conn: sqlite3.Connection,
) -> None:
    announcement_id = _vertical_announcement(conn)

    staged = fan_out_announcement(conn, announcement_id, now=_T0)

    rows = _notifications(conn)
    assert staged == 4
    assert [int(row["recipient_person_id"]) for row in rows] == [1, 2, 3, 4]
    for row in rows:
        assert str(row["event_type"]) == EVENT_ANNOUNCEMENT
        assert int(row["announcement_id"]) == announcement_id
        person_id = int(row["recipient_person_id"])
        assert str(row["dedupe_key"]) == announcement_key(announcement_id, person_id)


def test_ah_audience_is_the_ahs_active_volunteers(
    conn: sqlite3.Connection,
) -> None:
    announcement_id = _ah_announcement(conn, ah_person_id=2)

    staged = fan_out_announcement(conn, announcement_id, now=_T0)

    rows = _notifications(conn)
    assert staged == 2
    assert [int(row["recipient_person_id"]) for row in rows] == [3, 4]


def test_inactive_members_are_excluded(conn: sqlite3.Connection) -> None:
    _vertical_announcement(conn)

    fan_out_announcement(conn, 1, now=_T0)

    recipients = {int(row["recipient_person_id"]) for row in _notifications(conn)}
    assert 5 not in recipients


def test_ah_with_no_reports_stages_zero_rows_and_does_not_fall_back(
    conn: sqlite3.Connection,
) -> None:
    add_person(conn, 9, "AH Three", "AH", vertical_id=1)
    _ah_announcement(conn, ah_person_id=9)

    staged = fan_out_announcement(conn, 1, now=_T0)

    assert staged == 0
    assert _notifications(conn) == []


def test_fan_out_is_idempotent(conn: sqlite3.Connection) -> None:
    _vertical_announcement(conn)

    first = fan_out_announcement(conn, 1, now=_T0)
    second = fan_out_announcement(conn, 1, now="2026-11-01T10:05:00+00:00")

    assert first == 4
    assert second == 0
    assert len(_notifications(conn)) == 4


def test_sweep_recovers_an_announcement_with_no_rows(
    conn: sqlite3.Connection,
) -> None:
    _vertical_announcement(conn)

    swept = sweep_unfanned_announcements(conn)

    assert swept == (1,)
    assert len(_notifications(conn)) == 4
    assert sweep_unfanned_announcements(conn) == ()
    assert len(_notifications(conn)) == 4


def test_announcement_body_is_the_accepted_description_only(
    conn: sqlite3.Connection,
) -> None:
    _vertical_announcement(conn)
    fan_out_announcement(conn, 1, now=_T0)

    for row in _notifications(conn):
        view = render_notification(conn, int(row["id"]))
        assert view.body == _DESCRIPTION
        assert "private source chat context" not in view.body
        assert "bucket" not in view.body.lower()


def test_missing_announcement_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(AnnouncementNotFoundError):
        fan_out_announcement(conn, 999, now=_T0)
