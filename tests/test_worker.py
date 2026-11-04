"""Scheduler, tick and worker tests (W3; FR-BKT-01/05/06, NFR-REL-04/05/06).

The tick is the only live driver of the pipeline: it advances each group's
clock (closure), assigns the messages that have settled (``t + C <= clock``),
optionally runs the injected relevance and proposal passes, and records the
stage latency rows (NFR-PERF-02). The worker is a plain durable-state loop:
nothing is queued in memory, so a crash between ticks is recoverable and a
repeated tick is idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.bucketing import BucketParams
from backend.config import (
    ConfigError,
    PartSettings,
    load_config,
    load_worker_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.orchestration import pipeline_backlog
from backend.proposals import (
    ProposalPassResult,
    ProposalResponse,
)
from backend.relevance import (
    MessageDecision,
    RelevancePassResult,
    RelevanceRequest,
    RelevanceResponse,
)
from backend.worker import TickResult, run_pipeline_tick, run_worker

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMS = BucketParams(10, 5, 30, 3)  # SRS §10.1 test set: C=3


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "worker.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group(connection: sqlite3.Connection, group_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, f"chan-{group_id}"),
    )


def _message(
    connection: sqlite3.Connection,
    message_id: int,
    seconds: int,
    *,
    sender_person_id: int | None = None,
    event_timestamp: str | None = None,
    received_at: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (?, 'discord', ?, 1, ?, ?, ?, 'content')",
        (
            message_id,
            f"m-{message_id}",
            sender_person_id,
            _iso(seconds) if event_timestamp is None else event_timestamp,
            _iso(seconds) if received_at is None else received_at,
        ),
    )


def _tick(connection: sqlite3.Connection, seconds: int, **kwargs: Any) -> TickResult:
    return run_pipeline_tick(
        connection, now=_iso(seconds), params=_PARAMS, **kwargs
    )


def _state(connection: sqlite3.Connection, bucket_id: int) -> str:
    row = connection.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    return str(row["processing_state"])


def _buckets(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, start_event_timestamp, end_event_timestamp, processing_state"
        " FROM bucket ORDER BY id"
    ).fetchall()


class ScriptedClassifier:
    """Deterministic classifier double (stateless; records call order)."""

    def __init__(
        self,
        calls: list[str] | None = None,
        *,
        decision: str = "RELEVANT",
        fail: bool = False,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.decision = decision
        self.fail = fail
        self.requests: list[RelevanceRequest] = []

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        self.calls.append("classify")
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("classifier unavailable")
        return RelevanceResponse(
            model_id="stub-model",
            model_version="stub-v1",
            prompt_version="stub-prompt",
            decisions=tuple(
                MessageDecision(message_id=message.message_id, decision=self.decision)
                for message in request.messages
            ),
        )


class RecordingGenerator:
    """Generator double; an empty response is a valid generation (zero items)."""

    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls if calls is not None else []
        self.requests: list[Any] = []

    def generate(self, request: Any) -> ProposalResponse:
        self.calls.append("generate")
        self.requests.append(request)
        return ProposalResponse(
            model_id="stub-model",
            model_version="stub-v1",
            prompt_version="stub-prompt",
            proposals=(),
        )


# --- tick order and settle-then-assign ---------------------------------------


def test_tick_advances_the_clock_and_assigns_settled_messages(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 12)

    result = _tick(conn, 15)

    assert result.groups[0].clock_at == _iso(15)
    assert result.groups[0].advanced is True
    assert result.assigned == 2
    assert result.skipped == 0
    assert result.relevance is None
    assert result.proposals is None
    assert [bucket["start_event_timestamp"] for bucket in _buckets(conn)] == [
        _iso(0),
        _iso(12),
    ]
    # Assignment never closes; the next tick's clock advance does it.
    assert [_state(conn, bucket["id"]) for bucket in _buckets(conn)] == [
        "OPEN",
        "OPEN",
    ]

    second = _tick(conn, 15)
    assert second.groups[0].advanced is False
    assert second.groups[0].closed_bucket_ids == (1,)
    assert second.assigned == 0
    assert [_state(conn, bucket["id"]) for bucket in _buckets(conn)] == [
        "CLOSED",
        "OPEN",
    ]


def test_tick_leaves_unsettled_messages_for_the_next_tick(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)

    result = _tick(conn, 2)  # t + C = 3 > 2

    assert result.assigned == 0
    assert _buckets(conn) == []
    assert pipeline_backlog(conn).unassigned_messages == 1

    settled = _tick(conn, 3)
    assert settled.assigned == 1


def test_repeated_tick_is_idempotent(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)

    first = _tick(conn, 13)
    second = _tick(conn, 13)

    assert first.assigned == 1
    assert second.assigned == 0
    assert len(_buckets(conn)) == 1
    assert second.groups[0].closed_bucket_ids == (1,)


def test_skipped_messages_stay_visible_in_the_backlog(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0, event_timestamp="nonsense", received_at="nonsense")

    result = _tick(conn, 10)

    assert result.assigned == 0
    assert result.skipped == 1
    backlog = pipeline_backlog(conn)
    assert backlog.unassigned_messages == 1
    assert backlog.groups[0].unassigned_messages == 1


# --- optional AI stages ------------------------------------------------------


def _closed_bucket_with_eligible_sender(connection: sqlite3.Connection) -> int:
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'Tech')")
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    connection.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, active, roll_number)"
        " VALUES (1, 'Head Person', '900000000000000001', 'phone-1', 'Head', 1, 'roll-1')"
    )
    connection.execute("INSERT INTO person_vertical (person_id, vertical_id) VALUES (1, 1)")
    _message(connection, 1, 0, sender_person_id=1)
    _tick(connection, 3)
    _tick(connection, 13)
    bucket_id = int(_buckets(connection)[0]["id"])
    assert _state(connection, bucket_id) == "CLOSED"
    return bucket_id


def test_tick_runs_relevance_before_proposals(
    conn: sqlite3.Connection,
) -> None:
    bucket_id = _closed_bucket_with_eligible_sender(conn)
    calls: list[str] = []
    classifier = ScriptedClassifier(calls)
    generator = RecordingGenerator(calls)

    result = _tick(conn, 13, classifier=classifier, generator=generator)

    assert isinstance(result.relevance, RelevancePassResult)
    assert isinstance(result.proposals, ProposalPassResult)
    # The proposal pass only sees candidates after classification has run.
    assert calls == ["classify", "generate"]
    assert _state(conn, bucket_id) == "PROCESSED"
    assert classifier.requests[0].bucket_id == bucket_id


def test_tick_forwards_the_configured_part_sizes(
    conn: sqlite3.Connection,
) -> None:
    """W6/O9-6: the tick's PartSettings reach both AI passes (NFR-MNT-01)."""
    conn.execute("INSERT INTO vertical (id, name) VALUES (1, 'Tech')")
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    conn.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, active,"
        " roll_number) VALUES (1, 'Head Person', '900000000000000001',"
        " '+919800000001', 'Head', 1, 'roll-1')"
    )
    conn.execute("INSERT INTO person_vertical (person_id, vertical_id) VALUES (1, 1)")
    _message(conn, 1, 0, sender_person_id=1)
    _message(conn, 2, 1, sender_person_id=1)
    _message(conn, 3, 2, sender_person_id=1)
    _tick(conn, 5)
    _tick(conn, 13)

    classifier = ScriptedClassifier([])
    generator = RecordingGenerator([])

    _tick(
        conn,
        13,
        classifier=classifier,
        generator=generator,
        parts=PartSettings(
            relevance_max_messages_per_call=2,
            proposal_max_candidates_per_call=1,
        ),
    )

    assert [len(request.messages) for request in classifier.requests] == [2, 1]
    assert [len(request.candidate_messages) for request in generator.requests] == [
        1,
        1,
        1,
    ]
    for request in generator.requests:
        assert [message.message_id for message in request.context_messages] == [
            1,
            2,
            3,
        ]


def test_tick_without_models_runs_nothing_ai_specific(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _tick(conn, 3)
    _tick(conn, 13)

    result = _tick(conn, 13)

    assert result.relevance is None
    assert result.proposals is None
    assert conn.execute("SELECT COUNT(*) FROM processing_attempt").fetchone()[0] == 0


def test_tick_continues_when_the_classifier_raises(
    conn: sqlite3.Connection,
) -> None:
    """AC-13/NFR-REL-05: a failure is retryable, never a fake IRRELEVANT."""
    bucket_id = _closed_bucket_with_eligible_sender(conn)
    failing = ScriptedClassifier(fail=True)

    result = _tick(conn, 13, classifier=failing)

    assert isinstance(result.relevance, RelevancePassResult)
    assert _state(conn, bucket_id) == "AWAITING_RECOVERY"
    flags = conn.execute(
        "SELECT flag_unclassified FROM source_message WHERE id = 1"
    ).fetchone()[0]
    assert flags == 1
    assert conn.execute("SELECT COUNT(*) FROM relevance_decision").fetchone()[0] == 0

    recovered = _tick(conn, 13, classifier=ScriptedClassifier())
    assert isinstance(recovered.relevance, RelevancePassResult)
    assert _state(conn, bucket_id) == "PROCESSED"
    assert conn.execute("SELECT COUNT(*) FROM relevance_decision").fetchone()[0] == 1


# --- latency rows (NFR-PERF-02) ---------------------------------------------


def test_tick_records_ingestion_and_bucket_delay_rows(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)  # event time == receipt time
    _message(conn, 2, 1)

    _tick(conn, 10)

    rows = conn.execute(
        "SELECT metric, value_seconds, source_message_id"
        " FROM latency_measurement ORDER BY metric, source_message_id"
    ).fetchall()
    assert [
        (str(row["metric"]), float(row["value_seconds"]), int(row["source_message_id"]))
        for row in rows
    ] == [
        ("bucket delay", 10.0, 1),
        ("bucket delay", 9.0, 2),
        ("ingestion delay", 0.0, 1),
        ("ingestion delay", 0.0, 2),
    ]


# --- worker loop -------------------------------------------------------------


class RecordingStopEvent:
    """Stop-event double: records every wait and stops on the scripted result."""

    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return False

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        return self.results.pop(0)


def test_worker_waits_the_interval_and_passes_an_injected_instant(
    conn: sqlite3.Connection,
) -> None:
    ticks: list[str] = []
    stop = RecordingStopEvent([False, True])

    run_worker(
        conn,
        tick=lambda now: ticks.append(now),
        stop_event=stop,
        interval_seconds=2.0,
    )

    assert len(ticks) == 2
    assert stop.waits == [2.0, 2.0]
    assert datetime.fromisoformat(ticks[0]).tzinfo is not None


def test_worker_backs_off_after_a_failed_tick_and_recovers(
    conn: sqlite3.Connection,
) -> None:
    attempts: list[str] = []

    def tick(now: str) -> None:
        attempts.append(now)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")

    stop = RecordingStopEvent([False, False, True])

    run_worker(
        conn,
        tick=tick,
        stop_event=stop,
        interval_seconds=2.0,
    )

    assert len(attempts) == 3
    assert stop.waits == [1.0, 2.0, 2.0]


def test_worker_restart_replays_durable_state_without_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-worker.db"
    first = connect(path)
    apply_migrations(first)
    try:
        _group(first)
        _message(first, 1, 0)
        assert _tick(first, 13).assigned == 1
    finally:
        first.close()

    second = connect(path)
    try:
        restart = _tick(second, 13)
        assert restart.assigned == 0
        assert restart.groups[0].closed_bucket_ids == (1,)
        assert second.execute("SELECT COUNT(*) FROM bucket").fetchone()[0] == 1
    finally:
        second.close()


# --- worker configuration (NFR-MNT-01) --------------------------------------


def test_worker_settings_defaults() -> None:
    settings = load_worker_settings(load_config({}))
    assert settings.interval_seconds == 2.0
    assert settings.backoff_min_seconds == 1.0
    assert settings.backoff_max_seconds == 60.0


def test_worker_settings_are_read_from_configuration() -> None:
    settings = load_worker_settings(
        load_config(
            {
                "WORKER_INTERVAL_SECONDS": "5",
                "WORKER_BACKOFF_MIN_SECONDS": "2",
                "WORKER_BACKOFF_MAX_SECONDS": "30",
            }
        )
    )
    assert settings.interval_seconds == 5.0
    assert settings.backoff_min_seconds == 2.0
    assert settings.backoff_max_seconds == 30.0


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"WORKER_INTERVAL_SECONDS": "0"}, "positive"),
        ({"WORKER_INTERVAL_SECONDS": "-1"}, "positive"),
        ({"WORKER_INTERVAL_SECONDS": "soon"}, "number"),
        ({"WORKER_BACKOFF_MIN_SECONDS": "0"}, "positive"),
        (
            {
                "WORKER_BACKOFF_MIN_SECONDS": "10",
                "WORKER_BACKOFF_MAX_SECONDS": "5",
            },
            "at least",
        ),
    ],
)
def test_invalid_worker_settings_fail_loudly(
    values: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_worker_settings(load_config(values))
