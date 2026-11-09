"""Delivery worker: retry, backoff, attempts, terminal states (FR-NOT-04/05, AC-17).

The worker claims in one transaction, calls the provider outside every
transaction, and records the outcome in a short transaction. A failure never
rolls back the change that caused the notification, never creates a second
notification row, and never copies a token, phone number or body into an
attempt error. A crash after send but before the state update is recovered and
the duplicate is visible as a second attempt row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.config import (
    Config,
    ConfigError,
    NotificationSettings,
    load_notification_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    PushDeliveryError,
    deliver_pending,
    list_notifications,
    register_endpoint,
)
from tests.notification_fixtures import (
    add_assignee,
    add_notification,
    add_proposal,
    add_task,
    seed_message_chain,
    seed_roster,
)

TOKEN = "fake-token-delivery"
BODY_MARKER = "clip the wires"
_T0 = "2026-11-01T10:00:00+00:00"


class FakePushSender:
    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = list(outcomes or [])

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
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return "fake-provider-message"


class TransactionProbeSender(FakePushSender):
    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__()
        self.connection = connection
        self.in_transaction: list[bool] = []

    def send(self, **kwargs: Any) -> str | None:
        self.in_transaction.append(self.connection.in_transaction)
        return super().send(**kwargs)


class SimulatedCrash(BaseException):
    pass


class CrashingSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> str | None:
        self.calls.append(kwargs)
        raise SimulatedCrash()


def _settings(
    *, max_attempts: int = 5, base: float = 5.0, cap: float = 300.0
) -> NotificationSettings:
    return NotificationSettings(
        max_attempts=max_attempts,
        backoff_base_seconds=base,
        backoff_max_seconds=cap,
        batch_limit=50,
    )


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    seed_roster(connection)
    seed_message_chain(connection)
    add_proposal(connection, 1, description=BODY_MARKER)
    add_task(connection, 1, proposal_id=1, description=BODY_MARKER)
    add_assignee(connection, task_id=1, person_id=3, agreement="AWAITING")
    yield connection
    connection.close()


def _notification(
    connection: sqlite3.Connection,
    *,
    recipient_person_id: int = 3,
    created_at: str = _T0,
) -> int:
    return add_notification(
        connection,
        event_type="TASK_ASSIGNED",
        recipient_person_id=recipient_person_id,
        dedupe_key=f"TASK_ASSIGNED:1:{recipient_person_id}:{created_at}",
        created_at=created_at,
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
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT attempt_number, outcome, error, recorded_at FROM notification_attempt"
        " WHERE notification_id = ? ORDER BY attempt_number",
        (notification_id,),
    ).fetchall()


def _register(connection: sqlite3.Connection, person_id: int = 3) -> None:
    register_endpoint(connection, person_id=person_id, token=TOKEN, now=_T0)


def test_success_marks_sent_and_records_one_attempt(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    sender = FakePushSender()

    batch = deliver_pending(conn, push=sender, now=_T0)

    assert (batch.attempted, batch.sent, batch.retrying, batch.exhausted) == (1, 1, 0, 0)
    assert _state(conn, notification_id) == "SENT"
    attempts = _attempts(conn, notification_id)
    assert [(row["attempt_number"], row["outcome"]) for row in attempts] == [(1, "SENT")]
    assert len(sender.calls) == 1
    assert sender.calls[0]["token"] == TOKEN
    assert sender.calls[0]["body"].endswith(BODY_MARKER)
    assert sender.calls[0]["collapse_key"] == str(notification_id)


def test_provider_call_happens_outside_a_transaction(
    conn: sqlite3.Connection,
) -> None:
    _notification(conn)
    _register(conn)
    sender = TransactionProbeSender(conn)

    deliver_pending(conn, push=sender, now=_T0)

    assert sender.in_transaction == [False]
    assert conn.in_transaction is False


def test_transient_failure_retries_the_same_row_after_backoff(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    failure = PushDeliveryError(
        permanent=False, status_code=503, provider_code="UNAVAILABLE"
    )
    sender = FakePushSender([failure, "fake-provider-message"])

    first = deliver_pending(conn, push=sender, now=_T0, settings=_settings())
    early = deliver_pending(
        conn, push=sender, now="2026-11-01T10:00:04+00:00", settings=_settings()
    )
    on_time = deliver_pending(
        conn, push=sender, now="2026-11-01T10:00:05+00:00", settings=_settings()
    )

    assert (first.attempted, first.sent, first.retrying, first.exhausted) == (1, 0, 1, 0)
    assert (early.attempted, early.sent, early.retrying, early.exhausted) == (0, 0, 0, 0)
    assert (on_time.attempted, on_time.sent, on_time.retrying, on_time.exhausted) == (
        1,
        1,
        0,
        0,
    )
    assert _state(conn, notification_id) == "SENT"
    attempts = _attempts(conn, notification_id)
    assert [(row["attempt_number"], row["outcome"]) for row in attempts] == [
        (1, "FAILED"),
        (2, "SENT"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0] == 1


def test_backoff_is_capped(conn: sqlite3.Connection) -> None:
    _notification(conn)
    _register(conn)
    failure = PushDeliveryError(permanent=False, status_code=500, provider_code="ERR")
    sender = FakePushSender([failure, failure, "fake-provider-message"])
    settings = _settings(base=5.0, cap=12.0)

    deliver_pending(conn, push=sender, now=_T0, settings=settings)
    deliver_pending(conn, push=sender, now="2026-11-01T10:00:05+00:00", settings=settings)
    too_early = deliver_pending(
        conn, push=sender, now="2026-11-01T10:00:14+00:00", settings=settings
    )
    on_time = deliver_pending(
        conn, push=sender, now="2026-11-01T10:00:15+00:00", settings=settings
    )

    assert too_early.attempted == 0
    assert on_time.sent == 1
    assert len(sender.calls) == 3


def test_max_attempts_exhausts_the_row(conn: sqlite3.Connection) -> None:
    notification_id = _notification(conn)
    _register(conn)
    failure = PushDeliveryError(permanent=False, status_code=503, provider_code="BUSY")
    sender = FakePushSender([failure, failure, "unused"])

    first = deliver_pending(conn, push=sender, now=_T0, settings=_settings(max_attempts=2))
    second = deliver_pending(
        conn, push=sender, now="2026-11-01T10:00:05+00:00", settings=_settings(max_attempts=2)
    )
    third = deliver_pending(
        conn, push=sender, now="2026-11-01T10:10:00+00:00", settings=_settings(max_attempts=2)
    )

    assert first.retrying == 1
    assert second.exhausted == 1
    assert third.attempted == 0
    assert _state(conn, notification_id) == "EXHAUSTED"
    assert [row["outcome"] for row in _attempts(conn, notification_id)] == [
        "FAILED",
        "FAILED",
    ]
    assert len(sender.calls) == 2


def test_permanent_failure_exhausts_immediately(conn: sqlite3.Connection) -> None:
    notification_id = _notification(conn)
    _register(conn)
    sender = FakePushSender(
        [PushDeliveryError(permanent=True, status_code=403, provider_code="FORBIDDEN")]
    )

    batch = deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    assert (batch.attempted, batch.sent, batch.retrying, batch.exhausted) == (1, 0, 0, 1)
    assert _state(conn, notification_id) == "EXHAUSTED"
    assert [row["outcome"] for row in _attempts(conn, notification_id)] == ["FAILED"]
    assert len(sender.calls) == 1


def test_no_endpoint_is_exhausted_with_no_endpoint_outcome(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    sender = FakePushSender()

    batch = deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    assert (batch.attempted, batch.sent, batch.retrying, batch.exhausted) == (1, 0, 0, 1)
    assert _state(conn, notification_id) == "EXHAUSTED"
    attempts = _attempts(conn, notification_id)
    assert [(row["outcome"], row["error"]) for row in attempts] == [
        ("NO_ENDPOINT", "NO_ACTIVE_ENDPOINT")
    ]
    assert sender.calls == []


def test_inactive_recipient_is_exhausted_with_no_endpoint_outcome(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    conn.execute("UPDATE person SET active = 0 WHERE id = 3")
    sender = FakePushSender()

    batch = deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    assert batch.exhausted == 1
    assert _state(conn, notification_id) == "EXHAUSTED"
    attempts = _attempts(conn, notification_id)
    assert [(row["outcome"], row["error"]) for row in attempts] == [
        ("NO_ENDPOINT", "RECIPIENT_INACTIVE")
    ]
    assert sender.calls == []


def test_failed_delivery_does_not_roll_back_the_source_change(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    before = conn.execute("SELECT description FROM task WHERE id = 1").fetchone()
    assert before is not None
    sender = FakePushSender(
        [PushDeliveryError(permanent=True, status_code=400, provider_code="BAD")]
    )

    deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    after = conn.execute("SELECT description FROM task WHERE id = 1").fetchone()
    assert after is not None
    assert after["description"] == before["description"]
    assert _state(conn, notification_id) == "EXHAUSTED"


def test_error_is_sanitized_to_status_and_provider_code(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    sender = FakePushSender(
        [PushDeliveryError(permanent=False, status_code=503, provider_code="UNAVAILABLE")]
    )

    deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    (attempt,) = _attempts(conn, notification_id)
    error = str(attempt["error"])
    assert error == "HTTP 503 UNAVAILABLE"
    assert TOKEN not in error
    assert BODY_MARKER not in error


def test_raw_exception_text_is_not_copied_into_the_error(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    sender = FakePushSender([RuntimeError(f"token {TOKEN} rejected for {BODY_MARKER}")])

    deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    (attempt,) = _attempts(conn, notification_id)
    error = str(attempt["error"])
    assert error == "PROVIDER_ERROR"
    assert TOKEN not in error
    assert BODY_MARKER not in error


def test_crash_between_send_and_state_update_is_visible_in_attempts(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    _register(conn)
    crashing = CrashingSender()

    with pytest.raises(SimulatedCrash):
        deliver_pending(conn, push=crashing, now=_T0, settings=_settings())

    assert _state(conn, notification_id) == "SENDING"
    assert _attempts(conn, notification_id) == []
    assert len(crashing.calls) == 1

    normal = FakePushSender()
    batch = deliver_pending(conn, push=normal, now=_T0, settings=_settings())

    assert batch.sent == 1
    assert len(normal.calls) == 1
    assert _state(conn, notification_id) == "SENT"
    attempts = _attempts(conn, notification_id)
    assert [(row["attempt_number"], row["outcome"], row["error"]) for row in attempts] == [
        (1, "FAILED", "WORKER_CRASH_AFTER_SEND"),
        (2, "SENT", None),
    ]
    assert conn.execute("SELECT COUNT(*) FROM notification").fetchone()[0] == 1


def test_batch_limit_caps_claimed_rows(conn: sqlite3.Connection) -> None:
    first = _notification(conn, created_at="2026-11-01T09:50:00+00:00")
    second = _notification(conn, created_at="2026-11-01T09:51:00+00:00")
    third = _notification(conn, created_at="2026-11-01T09:52:00+00:00")
    _register(conn)
    sender = FakePushSender()

    batch = deliver_pending(conn, push=sender, limit=2, now=_T0, settings=_settings())

    assert batch.attempted == 2
    assert _state(conn, first) == "SENT"
    assert _state(conn, second) == "SENT"
    assert _state(conn, third) == "PENDING"
    assert len(sender.calls) == 2


def test_no_push_sender_claims_nothing(conn: sqlite3.Connection) -> None:
    notification_id = _notification(conn)

    batch = deliver_pending(conn, push=None, now=_T0, settings=_settings())

    assert (batch.attempted, batch.sent, batch.retrying, batch.exhausted) == (0, 0, 0, 0)
    assert _state(conn, notification_id) == "PENDING"


def test_exhausted_row_still_appears_unread_in_the_in_app_list(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification(conn)
    sender = FakePushSender()

    deliver_pending(conn, push=sender, now=_T0, settings=_settings())

    views = list_notifications(conn, person_id=3, unread_only=True)
    assert [view.notification_id for view in views] == [notification_id]
    assert views[0].read_at is None


def test_notification_settings_defaults_are_the_owner_values() -> None:
    settings = NotificationSettings()

    assert settings.max_attempts == 5
    assert settings.backoff_base_seconds == 5.0
    assert settings.backoff_max_seconds == 300.0
    assert settings.batch_limit == 50


def test_notification_settings_are_configuration() -> None:
    config = Config(
        {
            "NOTIFICATION_MAX_ATTEMPTS": "7",
            "NOTIFICATION_BACKOFF_BASE_SECONDS": "2.5",
            "NOTIFICATION_BACKOFF_MAX_SECONDS": "90",
            "NOTIFICATION_BATCH_LIMIT": "20",
        }
    )

    settings = load_notification_settings(config)

    assert settings.max_attempts == 7
    assert settings.backoff_base_seconds == 2.5
    assert settings.backoff_max_seconds == 90.0
    assert settings.batch_limit == 20


def test_notification_settings_blank_values_keep_defaults() -> None:
    config = Config(
        {
            "NOTIFICATION_MAX_ATTEMPTS": "",
            "NOTIFICATION_BACKOFF_BASE_SECONDS": " ",
            "NOTIFICATION_BACKOFF_MAX_SECONDS": "",
            "NOTIFICATION_BATCH_LIMIT": "",
        }
    )

    assert load_notification_settings(config) == NotificationSettings()


@pytest.mark.parametrize(
    "values",
    [
        {"NOTIFICATION_MAX_ATTEMPTS": "0"},
        {"NOTIFICATION_MAX_ATTEMPTS": "many"},
        {"NOTIFICATION_BACKOFF_BASE_SECONDS": "0"},
        {"NOTIFICATION_BACKOFF_BASE_SECONDS": "soon"},
        {"NOTIFICATION_BACKOFF_MAX_SECONDS": "0"},
        {"NOTIFICATION_BACKOFF_BASE_SECONDS": "10",
         "NOTIFICATION_BACKOFF_MAX_SECONDS": "9"},
        {"NOTIFICATION_BATCH_LIMIT": "0"},
        {"NOTIFICATION_BATCH_LIMIT": "many"},
    ],
)
def test_notification_settings_reject_invalid_values(
    values: dict[str, str],
) -> None:
    with pytest.raises(ConfigError):
        load_notification_settings(Config(values))
