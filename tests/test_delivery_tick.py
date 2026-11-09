"""Delivery tick wiring and the Session A carry-forward checks (W6).

The tick sweeps accepted announcements before delivering, uses the configured
batch/retry settings, and is restart-safe (no in-memory queue). The Phase 9
worker hook is optional: a delivery failure is logged and never fails the
pipeline tick.

The carry-forward tests document two deliberate limits: the delivery tick runs
in exactly one process, and an ``ASSIGNEE_ACCEPTED`` row names the most recent
assignee who accepted because the schema does not link the row to the actor.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.config import NotificationSettings
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    PushDeliveryError,
    RecordingOtpSender,
    deliver_pending,
    register_endpoint,
    render_notification,
    run_delivery_tick,
    sweep_unfanned_announcements,
)
from backend.worker import run_live_tick
from tests.notification_fixtures import (
    add_announcement,
    add_assignee,
    add_notification,
    add_person,
    add_proposal,
    add_task,
    seed_message_chain,
    seed_roster,
)

_T0 = "2026-11-01T10:00:00+00:00"
_T1 = "2026-11-01T10:00:05+00:00"


class FakePushSender:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def send(
        self,
        *,
        token: str,
        title: str,
        body: str,
        deep_link: str | None,
        collapse_key: str,
    ) -> str | None:
        self.calls.append(
            {
                "token": token,
                "title": title,
                "body": body,
                "deep_link": deep_link,
                "collapse_key": collapse_key,
            }
        )
        if self._error is not None:
            raise self._error
        return "fake-message"


class FakeSource:
    def fetch_messages(
        self, *, channel_id: str, after: str | None
    ) -> tuple[()]:
        return ()

    def fetch_reactions(
        self, *, channel_id: str, message_id: str
    ) -> tuple[()]:
        return ()

    def fetch_latest_page(self, *, channel_id: str) -> tuple[()]:
        return ()


class RecordingDelivery:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls = 0
        self._error = error

    def __call__(self) -> object:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return None


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "tick.db")
    apply_migrations(connection)
    seed_roster(connection)
    seed_message_chain(connection)
    add_proposal(connection, 1, description="clip the wires")
    add_task(connection, 1, proposal_id=1, description="clip the wires")
    add_assignee(connection, task_id=1, person_id=3, agreement="AWAITING")
    yield connection
    connection.close()


@pytest.fixture()
def worker_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "worker.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _settings(*, batch_limit: int = 50, max_attempts: int = 5) -> NotificationSettings:
    return NotificationSettings(
        max_attempts=max_attempts,
        backoff_base_seconds=5.0,
        backoff_max_seconds=300.0,
        batch_limit=batch_limit,
    )


def _register(connection: sqlite3.Connection, *person_ids: int) -> None:
    for person_id in person_ids:
        register_endpoint(
            connection,
            person_id=person_id,
            token=f"fake-token-{person_id}",
            now=_T0,
        )


def _notification(
    connection: sqlite3.Connection,
    *,
    recipient_person_id: int,
    dedupe_suffix: str = "one",
) -> int:
    return add_notification(
        connection,
        event_type="TASK_ASSIGNED",
        recipient_person_id=recipient_person_id,
        dedupe_key=f"TASK_ASSIGNED:1:{recipient_person_id}:{dedupe_suffix}",
        created_at=_T0,
        task_id=1,
    )


def _state(connection: sqlite3.Connection, notification_id: int) -> str:
    row = connection.execute(
        "SELECT delivery_state FROM notification WHERE id = ?", (notification_id,)
    ).fetchone()
    assert row is not None
    return str(row["delivery_state"])


def _attempts(
    connection: sqlite3.Connection, notification_id: int
) -> list[tuple[int, str, str | None]]:
    rows = connection.execute(
        "SELECT attempt_number, outcome, error FROM notification_attempt"
        " WHERE notification_id = ? ORDER BY attempt_number",
        (notification_id,),
    ).fetchall()
    return [
        (int(row["attempt_number"]), str(row["outcome"]), row["error"])
        for row in rows
    ]


def test_tick_sweeps_then_delivers_using_the_configured_batch_limit(
    conn: sqlite3.Connection,
) -> None:
    add_announcement(
        conn,
        1,
        proposal_id=1,
        accepted_by=1,
        audience_type="VERTICAL",
        audience_vertical_id=1,
    )
    _register(conn, 1, 2, 3, 4)
    sender = FakePushSender()

    first = run_delivery_tick(
        conn, push=sender, settings=_settings(batch_limit=2), now=_T0
    )

    assert (first.attempted, first.sent, first.retrying, first.exhausted) == (
        2,
        2,
        0,
        0,
    )
    count = conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()
    assert count is not None and int(count["n"]) == 4
    states = [
        str(row["delivery_state"])
        for row in conn.execute(
            "SELECT delivery_state FROM notification ORDER BY id"
        ).fetchall()
    ]
    assert states.count("SENT") == 2
    assert states.count("PENDING") == 2

    second = run_delivery_tick(
        conn, push=sender, settings=_settings(batch_limit=50), now=_T1
    )

    assert (second.attempted, second.sent) == (2, 2)
    count = conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()
    assert count is not None and int(count["n"]) == 4
    assert len(sender.calls) == 4


def test_explicit_limit_still_overrides_the_configured_batch_limit(
    conn: sqlite3.Connection,
) -> None:
    _notification(conn, recipient_person_id=3, dedupe_suffix="a")
    _notification(conn, recipient_person_id=4, dedupe_suffix="b")
    _notification(conn, recipient_person_id=1, dedupe_suffix="c")
    _register(conn, 1, 3, 4)
    sender = FakePushSender()

    tick_batch = run_delivery_tick(
        conn, push=sender, limit=1, settings=_settings(batch_limit=3), now=_T0
    )
    direct_batch = deliver_pending(
        conn, push=sender, limit=1, settings=_settings(batch_limit=3), now=_T1
    )

    assert tick_batch.attempted == 1
    assert direct_batch.attempted == 1
    assert len(sender.calls) == 2


def test_tick_uses_the_configured_max_attempts_for_a_terminal_failure(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn, recipient_person_id=3)
    _register(conn, 3)
    sender = FakePushSender(
        PushDeliveryError(
            permanent=False, status_code=503, provider_code="UNAVAILABLE"
        )
    )

    batch = run_delivery_tick(
        conn, push=sender, settings=_settings(max_attempts=1), now=_T0
    )

    assert (batch.attempted, batch.retrying, batch.exhausted) == (1, 0, 1)
    assert _state(conn, notification_id) == "EXHAUSTED"
    assert _attempts(conn, notification_id) == [
        (1, "FAILED", "HTTP 503 UNAVAILABLE")
    ]


def test_sending_recovery_documents_the_single_delivery_process_rule(
    conn: sqlite3.Connection,
) -> None:
    """A SENDING row is treated as a lost claim and sent again.

    If a second delivery process ran while the first was between claim and
    state update, it would recover the claim and send the same body twice. The
    deployment rule is therefore exactly one delivery process (the worker);
    the duplicate is visible as ``WORKER_CRASH_AFTER_SEND``.
    """
    notification_id = _notification(conn, recipient_person_id=3)
    _register(conn, 3)
    conn.execute(
        "UPDATE notification SET delivery_state = 'SENDING' WHERE id = ?",
        (notification_id,),
    )
    conn.execute(
        "INSERT INTO notification_attempt (notification_id, attempt_number,"
        " outcome, error, recorded_at) VALUES (?, 1, 'SENT', NULL, ?)",
        (notification_id, _T0),
    )
    sender = FakePushSender()

    batch = deliver_pending(conn, push=sender, now=_T1)

    assert batch.attempted == 1
    assert len(sender.calls) == 1
    assert _state(conn, notification_id) == "SENT"
    assert _attempts(conn, notification_id) == [
        (1, "SENT", None),
        (2, "FAILED", "WORKER_CRASH_AFTER_SEND"),
        (3, "SENT", None),
    ]


def test_zero_recipient_announcement_sweep_is_idempotent_and_cheap(
    conn: sqlite3.Connection,
) -> None:
    add_person(conn, 9, "AH No Reports", "AH", vertical_id=1)
    add_announcement(
        conn,
        2,
        proposal_id=1,
        accepted_by=9,
        audience_type="AH_VOLUNTEERS",
        audience_vertical_id=1,
        audience_ah_person_id=9,
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    before = conn.total_changes

    first = sweep_unfanned_announcements(conn)
    second = sweep_unfanned_announcements(conn)
    conn.set_trace_callback(None)

    assert first == ()
    assert second == ()
    count = conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()
    assert count is not None and int(count["n"]) == 0
    assert conn.total_changes == before
    assert (
        sum(
            "SELECT a.id FROM announcement" in statement
            for statement in statements
        )
        == 2
    )
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )


def test_two_acceptances_cannot_be_distinguished_by_the_notification_row(
    conn: sqlite3.Connection,
) -> None:
    """The row stores the task and recipient, not which assignee acted.

    Both assignees accept, but each notification renders the assignee with the
    most recent ``agreed_at`` for the agreement. The limitation is recorded in
    section 15 and KNOWN_GAPS; the wording is not changed to hide it.
    """
    conn.execute(
        "UPDATE task_assignee SET agreement = 'ACCEPTED', agreed_at = ?"
        " WHERE task_id = 1 AND person_id = 3",
        ("2026-11-01T10:01:00+00:00",),
    )
    add_assignee(
        conn,
        task_id=1,
        person_id=4,
        agreement="ACCEPTED",
        agreed_at="2026-11-01T10:02:00+00:00",
    )
    first_id = add_notification(
        conn,
        event_type="ASSIGNEE_ACCEPTED",
        recipient_person_id=2,
        dedupe_key="ASSIGNEE_ACCEPTED:1:2:first",
        created_at=_T0,
        task_id=1,
    )
    second_id = add_notification(
        conn,
        event_type="ASSIGNEE_ACCEPTED",
        recipient_person_id=2,
        dedupe_key="ASSIGNEE_ACCEPTED:1:2:second",
        created_at=_T1,
        task_id=1,
    )

    first = render_notification(conn, first_id)
    second = render_notification(conn, second_id)

    assert first.body == "Volunteer Two accepted the task: clip the wires"
    assert second.body == first.body


def test_tick_accepts_an_otp_sender_without_inventing_otp_notification_rows(
    conn: sqlite3.Connection,
) -> None:
    otp_sender = RecordingOtpSender()

    batch = run_delivery_tick(
        conn, push=None, otp_sender=otp_sender, settings=_settings(), now=_T0
    )

    assert (batch.attempted, batch.sent, batch.retrying, batch.exhausted) == (
        0,
        0,
        0,
        0,
    )
    assert otp_sender.sent == ()


def test_worker_delivery_hook_failure_does_not_fail_the_pipeline_tick(
    worker_conn: sqlite3.Connection,
) -> None:
    worker_conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-tick')"
    )
    delivery = RecordingDelivery(RuntimeError("push failed"))

    result = run_live_tick(
        worker_conn,
        source=FakeSource(),
        now=lambda: _T0,
        delivery_tick=delivery,
    )

    assert delivery.calls == 1
    assert len(result.tick.groups) == 1
    assert result.tick.assigned == 0


def test_worker_delivery_hook_is_optional_and_off_by_default(
    worker_conn: sqlite3.Connection,
) -> None:
    worker_conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-tick')"
    )
    delivery = RecordingDelivery()

    result = run_live_tick(
        worker_conn,
        source=FakeSource(),
        now=lambda: _T0,
        delivery_tick=None,
    )

    assert delivery.calls == 0
    assert len(result.tick.groups) == 1
