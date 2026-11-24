"""The two system pause switches (owner decision C, Phase 12 D2).

Processing and delivery pause independently, and ingestion is not one of the
switches. The service re-checks the caller's current roster role even though the
route gate already does, and every write is one row per control so a repeated
pause is idempotent.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from backend.authorization import AuthorizationError
from backend.bucketing import BucketParams
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import register_endpoint, run_delivery_tick
from backend.relevance import MessageDecision, RelevanceResponse
from backend.system_control import (
    DELIVERY,
    PROCESSING,
    InvalidControlError,
    SystemControlAuthorizationError,
    is_delivery_paused,
    is_processing_paused,
    pause,
    read_state,
    resume,
)
from backend.worker import run_live_tick, run_pipeline_tick
from tests.notification_fixtures import (
    add_notification,
    add_proposal,
    add_task,
    seed_message_chain,
    seed_roster,
)

_NOW = "2026-11-18T10:00:00+00:00"


class _ForbiddenClassifier:
    """A classifier that fails if the pause actually calls a model."""

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, request: object) -> RelevanceResponse:
        self.calls += 1
        raise AssertionError("classifier must not be called while paused")


class _ForbiddenGenerator:
    """A generator that fails if the pause actually calls a model."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: object) -> object:
        self.calls += 1
        raise AssertionError("generator must not be called while paused")


class _RecordingClassifier:
    """Return IRRELEVANT for every message and record the bucket order."""

    def __init__(self) -> None:
        self.bucket_ids: list[int] = []

    def classify(self, request: Any) -> RelevanceResponse:
        self.bucket_ids.append(int(request.bucket_id))
        return RelevanceResponse(
            model_id="model-r",
            model_version="1.0.0",
            prompt_version="rel-prompt-1",
            decisions=tuple(
                MessageDecision(
                    message_id=int(message.message_id),
                    decision="IRRELEVANT",
                )
                for message in request.messages
            ),
        )


class _EmptySource:
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


class _FakePush:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(
        self,
        *,
        token: str,
        title: str,
        body: str,
        deep_link: str | None,
        collapse_key: str,
    ) -> str | None:
        self.calls.append(token)
        return "fake-message"


def _seed_settled_message(connection: sqlite3.Connection, *, group_id: int = 1) -> None:
    """One message old enough that the next tick settles and closes it."""
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, f"chan-{group_id}"),
    )
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content)"
        " VALUES (?, 'discord', ?, ?, '2026-11-18T09:00:00+00:00',"
        " '2026-11-18T09:00:00+00:00', 'content')",
        (group_id, f"m-{group_id}", group_id),
    )


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "system.db")
    apply_migrations(connection)
    seed_roster(connection)
    yield connection
    connection.close()


def test_both_switches_default_to_resumed(conn: sqlite3.Connection) -> None:
    state = read_state(conn)
    assert state.processing_paused is False
    assert state.delivery_paused is False
    assert is_processing_paused(conn) is False
    assert is_delivery_paused(conn) is False


def test_processing_and_delivery_pause_independently(conn: sqlite3.Connection) -> None:
    paused = pause(
        conn, control=PROCESSING, actor_person_id=6, now=_NOW
    )
    assert paused.processing_paused is True
    assert paused.delivery_paused is False

    both = pause(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    assert both.processing_paused is True
    assert both.delivery_paused is True

    processed = resume(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    assert processed.processing_paused is False
    assert processed.delivery_paused is True

    resumed = resume(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    assert resumed.processing_paused is False
    assert resumed.delivery_paused is False


def test_a_repeated_pause_keeps_one_row_and_stays_paused(
    conn: sqlite3.Connection,
) -> None:
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    rows = conn.execute(
        "SELECT name, paused FROM system_control ORDER BY name"
    ).fetchall()
    assert [(row["name"], row["paused"]) for row in rows] == [("PROCESSING", 1)]


def test_a_non_admin_cannot_change_a_switch_and_the_denial_is_recorded(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(SystemControlAuthorizationError) as caught:
        pause(conn, control=PROCESSING, actor_person_id=1, now=_NOW)
    assert caught.value.reason == "NOT_ROLE"
    row = conn.execute(
        "SELECT * FROM authorization_denial ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["operation"] == "SYSTEM_PAUSE"
    assert row["reason"] == "NOT_ROLE"
    assert is_processing_paused(conn) is False


def test_an_inactive_admin_cannot_change_a_switch(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE person SET active = 0 WHERE id = 6")
    with pytest.raises(AuthorizationError):
        resume(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    assert is_delivery_paused(conn) is False


def test_an_unknown_switch_is_rejected_without_a_write(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(InvalidControlError):
        pause(conn, control="EVERYTHING", actor_person_id=6, now=_NOW)
    assert conn.execute("SELECT COUNT(*) AS n FROM system_control").fetchone()["n"] == 0


def test_a_processing_pause_skips_model_calls_but_still_closes_buckets(
    conn: sqlite3.Connection,
) -> None:
    """Owner decision C: ingestion, clocks and bucket closure keep running."""
    _seed_settled_message(conn)
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    classifier = _ForbiddenClassifier()
    generator = _ForbiddenGenerator()

    formed = run_pipeline_tick(
        conn, now=_NOW, classifier=classifier, generator=generator
    )
    closed = run_pipeline_tick(
        conn, now=_NOW, classifier=classifier, generator=generator
    )

    assert len(formed.groups) == 1
    assert formed.assigned == 1
    assert len(closed.groups) == 1
    assert closed.assigned == 0
    assert formed.relevance is None
    assert closed.relevance is None
    assert formed.proposals is None
    assert closed.proposals is None
    assert classifier.calls == 0
    assert generator.calls == 0
    bucket = conn.execute(
        "SELECT processing_state FROM bucket"
    ).fetchone()
    assert bucket is not None
    assert bucket["processing_state"] == "CLOSED"


def test_resuming_processing_processes_the_closed_backlog_in_order(
    conn: sqlite3.Connection,
) -> None:
    """The backlog formed while paused is processed once, in bucket order."""
    _seed_settled_message(conn, group_id=1)
    _seed_settled_message(conn, group_id=2)
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)

    paused = run_pipeline_tick(
        conn, now=_NOW, classifier=_ForbiddenClassifier()
    )
    run_pipeline_tick(conn, now=_NOW, classifier=_ForbiddenClassifier())
    assert paused.relevance is None
    assert conn.execute("SELECT COUNT(*) AS n FROM relevance_decision").fetchone()[
        "n"
    ] == 0
    states = [
        str(row["processing_state"])
        for row in conn.execute(
            "SELECT processing_state FROM bucket ORDER BY id"
        ).fetchall()
    ]
    assert states == ["CLOSED", "CLOSED"]

    resume(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    classifier = _RecordingClassifier()
    resumed = run_pipeline_tick(conn, now=_NOW, classifier=classifier)

    assert classifier.bucket_ids == [1, 2]
    assert resumed.relevance is not None
    states = [
        str(row["processing_state"])
        for row in conn.execute(
            "SELECT processing_state FROM bucket ORDER BY id"
        ).fetchall()
    ]
    assert states == ["PROCESSED", "PROCESSED"]
    assert conn.execute("SELECT COUNT(*) AS n FROM relevance_decision").fetchone()[
        "n"
    ] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM processing_attempt").fetchone()[
        "n"
    ] == 2


def test_a_delivery_pause_stages_notifications_and_resume_sends_each_once(
    conn: sqlite3.Connection,
) -> None:
    """Owner decision C: delivery pauses without losing or duplicating a send."""
    seed_message_chain(conn)
    conn.execute(
        "UPDATE bucket SET parameters_used = ? WHERE id = 1",
        (BucketParams().as_json(),),
    )
    add_proposal(conn, 1, description="clip the wires")
    add_task(conn, 1, proposal_id=1, description="clip the wires")
    notification_id = add_notification(
        conn,
        event_type="TASK_ASSIGNED",
        recipient_person_id=3,
        dedupe_key="pause-delivery-1",
        created_at=_NOW,
        task_id=1,
    )
    register_endpoint(conn, person_id=3, token="fake-token-3", now=_NOW)
    push = _FakePush()

    def delivery() -> object:
        return run_delivery_tick(conn, push=push, now=_NOW)

    pause(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    run_live_tick(
        conn, source=_EmptySource(), now=lambda: _NOW, delivery_tick=delivery
    )
    assert push.calls == []
    assert _delivery_state(conn, notification_id) == "PENDING"

    resume(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    run_live_tick(
        conn, source=_EmptySource(), now=lambda: _NOW, delivery_tick=delivery
    )
    assert len(push.calls) == 1
    assert _delivery_state(conn, notification_id) == "SENT"

    run_live_tick(
        conn, source=_EmptySource(), now=lambda: _NOW, delivery_tick=delivery
    )
    assert len(push.calls) == 1


def test_an_unreadable_switch_state_skips_optional_stages_and_logs_once(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken switch read must not stop ingestion or raise out of the tick."""
    _seed_settled_message(conn)
    conn.execute("DROP TABLE system_control")
    classifier = _ForbiddenClassifier()
    delivery_calls: list[int] = []

    with caplog.at_level(logging.WARNING, logger="asec.system_control"):
        result = run_live_tick(
            conn,
            source=_EmptySource(),
            now=lambda: _NOW,
            classifier=classifier,
            delivery_tick=lambda: delivery_calls.append(1),
        )

    assert classifier.calls == 0
    assert delivery_calls == []
    assert len(result.tick.groups) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM bucket").fetchone()["n"] == 1
    records = [
        record
        for record in caplog.records
        if record.name == "asec.system_control" and record.levelno == logging.WARNING
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "unreadable" in message.lower()
    assert "content" not in message


def test_setting_a_switch_to_its_current_value_writes_nothing(
    conn: sqlite3.Connection,
) -> None:
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    later = "2026-11-18T11:00:00+00:00"

    pause(conn, control=PROCESSING, actor_person_id=6, now=later)

    row = conn.execute(
        "SELECT updated_at, updated_by FROM system_control"
        " WHERE name = 'PROCESSING'"
    ).fetchone()
    assert row is not None
    assert row["updated_at"] == _NOW
    assert row["updated_by"] == 6
    denials = conn.execute(
        "SELECT COUNT(*) AS n FROM authorization_denial"
    ).fetchone()
    assert denials is not None and denials["n"] == 0


def test_resuming_an_already_resumed_switch_writes_no_row(
    conn: sqlite3.Connection,
) -> None:
    resume(conn, control=PROCESSING, actor_person_id=6, now=_NOW)

    assert conn.execute("SELECT COUNT(*) AS n FROM system_control").fetchone()[
        "n"
    ] == 0


def _delivery_state(connection: sqlite3.Connection, notification_id: int) -> str:
    row = connection.execute(
        "SELECT delivery_state FROM notification WHERE id = ?",
        (notification_id,),
    ).fetchone()
    assert row is not None
    return str(row["delivery_state"])
