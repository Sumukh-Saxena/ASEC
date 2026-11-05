"""W8a live orchestration tests (A1 poll-bounded clock, A2 live driver).

The live driver polls every mapped group through an injected
:class:`~backend.discord_adapter.DiscordSource`, stores each fetched message
through the normal ingestion path, records the successful poll instant only
after the page is committed, and only then advances that group's clock to
``min(now, polled_at - poll_safety_margin_seconds)``. A stalled or failed poll
leaves the group's clock untouched, so no bucket is closed before its messages
are fetched (PB-1, FR-BKT-05/06, NFR-REL-04). All network access is scripted:
the suite makes no real request.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.alerts import POLL_FAILURE_RULE, Alert
from backend.bucketing import BucketParams
from backend.clock import advance_group_clock, read_group_clock
from backend.config import (
    DEFAULT_POLL_SAFETY_MARGIN_SECONDS,
    ConfigError,
    DiscordSettings,
    load_config,
    load_worker_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.discord_adapter import DiscordRestSource, HttpResponse, read_cursor
from backend.worker import run_live_tick, run_pipeline_tick

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMS = BucketParams(15, 5, 30, 2)  # SRS §10.1: W=15, G=5, M=30, C=2
_TS = "2026-01-01T00:00:00+00:00"
_FAKE_TOKEN = "test-token-not-a-secret"


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


class FakeClock:
    """Injected wall-clock boundary; tests set ``value`` between ticks."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self.value


class FakeTransport:
    """Scripted transport keyed by channel; never touches the network."""

    def __init__(
        self,
        responder: Callable[[str, str | None], bytes | Exception],
    ) -> None:
        self._responder = responder
        self.requests: list[dict[str, Any]] = []

    def request(
        self, *, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        channel = url.split("/channels/", 1)[1].split("/", 1)[0]
        after = url.rsplit("after=", 1)[1] if "after=" in url else None
        self.requests.append(
            {
                "channel": channel,
                "after": after,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        result = self._responder(channel, after)
        if isinstance(result, Exception):
            raise result
        return HttpResponse(status=200, headers={}, body=result)


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class RecordingSender:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


def _entry(message_id: str, seconds: int, channel_id: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "content": f"message {message_id}",
        "timestamp": _iso(seconds),
        "author": {"id": "user-1"},
    }


def _page(
    channel_id: str,
    messages: Sequence[tuple[str, int]],
    *,
    after: str | None,
) -> bytes:
    """Return a page filtered by the cursor, like Discord's ``after`` query."""
    floor = int(after) if after is not None else -1
    selected = [pair for pair in messages if int(pair[0]) > floor]
    return json.dumps(
        [_entry(message_id, seconds, channel_id) for message_id, seconds in selected]
    ).encode("utf-8")


def _source(transport: FakeTransport) -> DiscordRestSource:
    return DiscordRestSource(
        token=_FAKE_TOKEN,
        transport=transport,
        settings=DiscordSettings(max_attempts=1),
        sleep=RecordingSleep(),
        now=lambda: _TS,
    )


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "live.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group(connection: sqlite3.Connection, group_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, f"chan-{group_id}"),
    )


def _clock(connection: sqlite3.Connection, group_id: int = 1) -> str | None:
    stored = read_group_clock(connection, group_id)
    return None if stored is None else stored.clock_at


def _states(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT processing_state FROM bucket ORDER BY id"
    ).fetchall()
    return [str(row["processing_state"]) for row in rows]


def _links(connection: sqlite3.Connection) -> list[int | None]:
    rows = connection.execute(
        "SELECT predecessor_bucket_id FROM bucket ORDER BY id"
    ).fetchall()
    return [None if row[0] is None else int(row[0]) for row in rows]


def _assigned_event_times(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT m.event_timestamp FROM bucket_message bm"
        " JOIN source_message m ON m.id = bm.message_id"
        " ORDER BY m.event_timestamp, m.id"
    ).fetchall()
    return [str(row[0]) for row in rows]


# --- A1: the poll-bounded clock ---------------------------------------------


def test_tick_advances_only_to_poll_instant_minus_margin(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)

    result = run_pipeline_tick(
        conn,
        now=_iso(100),
        poll_instants={1: _iso(100)},
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert result.groups[0].clock_at == _iso(98)
    assert _clock(conn) == _iso(98)


def test_tick_never_advances_past_now(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)

    result = run_pipeline_tick(
        conn,
        now=_iso(50),
        poll_instants={1: _iso(100)},
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert result.groups[0].clock_at == _iso(50)


def test_tick_without_a_successful_poll_leaves_the_clock_unchanged(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    advance_group_clock(conn, 1, now=_iso(10), mode="LIVE")

    result = run_pipeline_tick(
        conn,
        now=_iso(500),
        poll_instants={},
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert result.groups == ()
    assert _clock(conn) == _iso(10)


def test_tick_does_not_advance_a_replay_group(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    advance_group_clock(conn, 1, now=_iso(10), mode="REPLAY")

    result = run_pipeline_tick(
        conn,
        now=_iso(500),
        poll_instants={1: _iso(500)},
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert result.groups == ()
    stored = read_group_clock(conn, 1)
    assert stored is not None
    assert (stored.clock_at, stored.mode) == (_iso(10), "REPLAY")


def test_poll_safety_margin_is_validated_configuration() -> None:
    settings = load_worker_settings(load_config({}))
    assert settings.poll_safety_margin_seconds == DEFAULT_POLL_SAFETY_MARGIN_SECONDS
    assert DEFAULT_POLL_SAFETY_MARGIN_SECONDS == 2.0

    configured = load_worker_settings(
        load_config({"POLL_SAFETY_MARGIN_SECONDS": "0"})
    )
    assert configured.poll_safety_margin_seconds == 0.0

    with pytest.raises(ConfigError, match="non-negative"):
        load_worker_settings(load_config({"POLL_SAFETY_MARGIN_SECONDS": "-1"}))


# --- A2: live orchestration -------------------------------------------------


def test_stalled_poll_does_not_advance_or_close(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    calls = {"count": 0}
    messages = [("1001", 0)]

    def responder(channel: str, after: str | None) -> bytes | Exception:
        calls["count"] += 1
        if calls["count"] == 1:
            return _page(channel, messages, after=after)
        return OSError("stalled adapter")

    clock = FakeClock(_iso(100))
    source = _source(FakeTransport(responder))

    first = run_live_tick(
        conn,
        source=source,
        now=clock,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )
    assert first.polls[0].failed is False
    assert _clock(conn) == _iso(98)
    assert _states(conn) == ["OPEN"]

    clock.value = _iso(200)
    stalled = run_live_tick(
        conn,
        source=source,
        now=clock,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert stalled.polls[0].failed is True
    assert stalled.polls[0].polled_at is None
    assert _clock(conn) == _iso(98)
    assert _states(conn) == ["OPEN"]


def test_recovery_closes_buckets_in_event_time_order_without_late_links(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    calls = {"count": 0}
    messages = [("1001", 0)]

    def responder(channel: str, after: str | None) -> bytes | Exception:
        calls["count"] += 1
        if calls["count"] == 2:
            return OSError("stalled adapter")
        if calls["count"] >= 3:
            messages.extend([("1002", 20), ("1003", 21)])
        return _page(channel, messages, after=after)

    clock = FakeClock(_iso(100))
    source = _source(FakeTransport(responder))

    run_live_tick(
        conn,
        source=source,
        now=clock,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )
    clock.value = _iso(200)
    stalled = run_live_tick(
        conn,
        source=source,
        now=clock,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )
    assert stalled.polls[0].failed is True

    clock.value = _iso(300)
    recovered = run_live_tick(
        conn,
        source=source,
        now=clock,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert recovered.polls[0].failed is False
    assert recovered.polls[0].fetched == 2
    assert recovered.polls[0].stored == 2
    assert _clock(conn) == _iso(298)
    # The pre-stall bucket closes; the recovered messages form the next bucket.
    assert _states(conn) == ["CLOSED", "OPEN"]
    assert _assigned_event_times(conn) == [_iso(0), _iso(20), _iso(21)]
    # Neither bucket is late, so no predecessor link may be invented.
    assert _links(conn) == [None, None]


def test_restart_holds_until_the_first_successful_poll(tmp_path: Path) -> None:
    path = tmp_path / "restart-live.db"
    first = connect(path)
    apply_migrations(first)
    try:
        _group(first)
        advance_group_clock(first, 1, now=_iso(10), mode="LIVE")
    finally:
        first.close()

    second = connect(path)
    try:
        # No poll has happened in this process: a tick must not advance.
        held = run_pipeline_tick(
            second,
            now=_iso(500),
            poll_instants={},
            poll_safety_margin_seconds=2,
            params=_PARAMS,
        )
        assert held.groups == ()
        assert _clock(second) == _iso(10)

        transport = FakeTransport(lambda channel, after: b"[]")
        source = _source(transport)
        advanced = run_live_tick(
            second,
            source=source,
            now=FakeClock(_iso(500)),
            poll_safety_margin_seconds=2,
            params=_PARAMS,
        )
        assert advanced.polls[0].failed is False
        assert _clock(second) == _iso(498)
    finally:
        second.close()


def test_one_failing_group_does_not_block_the_others(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1)
    _group(conn, 2)

    def responder(channel: str, after: str | None) -> bytes | Exception:
        if channel == "chan-1":
            return OSError("channel unavailable")
        return _page(channel, [("2001", 0)], after=after)

    source = _source(FakeTransport(responder))
    result = run_live_tick(
        conn,
        source=source,
        now=FakeClock(_iso(100)),
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    by_group = {poll.group_id: poll for poll in result.polls}
    assert by_group[1].failed is True
    assert by_group[2].failed is False
    assert by_group[2].stored == 1
    assert _clock(conn, 1) is None
    assert _clock(conn, 2) == _iso(98)
    assert read_cursor(conn, channel_id="chan-2") == "2001"


def test_live_tick_skips_a_replay_group_without_polling_it(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    advance_group_clock(conn, 1, now=_iso(10), mode="REPLAY")

    def responder(channel: str, after: str | None) -> bytes | Exception:
        raise AssertionError("a REPLAY group must not be polled live")

    source = _source(FakeTransport(responder))
    result = run_live_tick(
        conn,
        source=source,
        now=FakeClock(_iso(500)),
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert result.polls[0].skipped_replay is True
    stored = read_group_clock(conn, 1)
    assert stored is not None
    assert (stored.clock_at, stored.mode) == (_iso(10), "REPLAY")


def test_repeated_poll_failures_alert_through_the_sender(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)

    def responder(channel: str, after: str | None) -> bytes | Exception:
        return OSError("channel unavailable")

    sender = RecordingSender()
    source = _source(FakeTransport(responder))
    failures: dict[int, int] = {}

    first = run_live_tick(
        conn,
        source=source,
        now=FakeClock(_iso(100)),
        alert_sender=sender,
        consecutive_failures=failures,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )
    assert first.polls[0].failed is True
    assert sender.alerts == []

    second = run_live_tick(
        conn,
        source=source,
        now=FakeClock(_iso(102)),
        alert_sender=sender,
        consecutive_failures=failures,
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert second.polls[0].failed is True
    assert failures == {1: 2}
    assert len(sender.alerts) == 1
    assert sender.alerts[0].rule == POLL_FAILURE_RULE
    assert sender.alerts[0].observed == 2


def test_poll_instants_are_recorded_after_ingestion(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)

    def responder(channel: str, after: str | None) -> bytes | Exception:
        return _page(channel, [("1001", 0), ("1002", 1)], after=after)

    source = _source(FakeTransport(responder))
    result = run_live_tick(
        conn,
        source=source,
        now=FakeClock(_iso(50)),
        poll_safety_margin_seconds=2,
        params=_PARAMS,
    )

    assert result.polls[0].polled_at == _iso(50)
    count = conn.execute("SELECT COUNT(*) FROM source_message").fetchone()[0]
    assert count == 2
    assert _clock(conn) == _iso(48)
