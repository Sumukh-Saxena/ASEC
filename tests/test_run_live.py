"""W8b run-live command tests (A3/A4).

The live command is the process boundary: it reads the token through
:meth:`Config.require`, refuses the configured production database as a
``--db`` argument and any database already locked to replay, checks the Live
Message Content readiness once, and then drives :func:`run_live_tick` until it
is interrupted. Every request goes through an injected fake transport, so the
suite stays offline and never needs a real token.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from backend.__main__ import PRODUCTION_DB_PATH_KEY, main
from backend.clock import advance_group_clock
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.discord_adapter import HttpResponse

TS = "2026-01-01T00:00:00+00:00"
TOKEN = "test-token-not-a-secret"


class FakeTransport:
    """Scripted transport: each request consumes the next response."""

    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(
        self, *, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        self.requests.append(
            {"url": url, "headers": dict(headers), "timeout": timeout}
        )
        if not self._responses:
            raise AssertionError("unexpected request: no scripted response left")
        return self._responses.pop(0)


class InterruptingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        raise KeyboardInterrupt


def _http(body: bytes = b"[]") -> HttpResponse:
    return HttpResponse(status=200, headers={}, body=body)


def _entry(message_id: str, content: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": "chan-1",
        "content": content,
        "timestamp": TS,
        "type": 0,
        "author": {"id": "user-1", "bot": False},
    }


def _json(messages: Sequence[Mapping[str, Any]]) -> bytes:
    return json.dumps(list(messages)).encode("utf-8")


def _config(database: Path | None = None) -> Config:
    values = {"DISCORD_BOT_TOKEN": TOKEN}
    if database is not None:
        values[PRODUCTION_DB_PATH_KEY] = str(database)
    return Config(values)


def _seeded_database(tmp_path: Path, name: str = "live.db") -> Path:
    path = tmp_path / name
    connection = connect(path)
    try:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
        )
    finally:
        connection.close()
    return path


def _replay_database(tmp_path: Path) -> Path:
    path = _seeded_database(tmp_path, "replay.db")
    connection = connect(path)
    try:
        advance_group_clock(connection, 1, now=TS, mode="REPLAY")
    finally:
        connection.close()
    return path


def test_run_live_once_uses_asec_db_path_and_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _seeded_database(tmp_path)
    body = _json([_entry("1", "hello from the channel")])
    transport = FakeTransport([_http(body), _http(body)])

    code = main(
        ["run-live", "--once"],
        config=_config(database),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "fetched=1" in captured.out
    assert "stored=1" in captured.out
    assert "duplicates=0" in captured.out
    assert "hello from the channel" not in captured.out
    assert "user-1" not in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert len(transport.requests) == 2

    connection = connect(database)
    try:
        count = connection.execute("SELECT COUNT(*) FROM source_message").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_run_live_refuses_the_production_database_path_after_resolving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "live.db"
    alias = tmp_path / "nested" / ".." / "live.db"
    transport = FakeTransport([])

    code = main(
        ["run-live", "--db", str(alias), "--once"],
        config=_config(database),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert code != 0
    assert "production" in captured.err.lower()
    assert transport.requests == []


def test_run_live_refuses_a_replay_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _replay_database(tmp_path)
    transport = FakeTransport([])

    code = main(
        ["run-live", "--db", str(database), "--once"],
        config=_config(),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert code != 0
    assert "REPLAY" in captured.err
    assert transport.requests == []


def test_run_live_without_db_requires_asec_db_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = FakeTransport([])

    code = main(
        ["run-live", "--once"],
        config=_config(),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert code != 0
    assert PRODUCTION_DB_PATH_KEY in captured.err
    assert transport.requests == []


def test_run_live_refuses_an_all_empty_latest_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _seeded_database(tmp_path)
    body = _json([_entry("1", ""), _entry("2", "")])
    transport = FakeTransport([_http(body)])

    code = main(
        ["run-live", "--db", str(database), "--once"],
        config=_config(),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert code != 0
    assert "Message Content" in captured.err
    assert len(transport.requests) == 1


def test_run_live_uses_the_two_second_default_and_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _seeded_database(tmp_path)
    transport = FakeTransport([_http(), _http()])
    sleep = InterruptingSleep()

    code = main(
        ["run-live", "--db", str(database)],
        config=_config(),
        transport=transport,
        sleep=sleep,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert sleep.calls == [2.0]
    assert "fetched=0" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_run_live_interval_seconds_argument_is_used(
    tmp_path: Path,
) -> None:
    database = _seeded_database(tmp_path)
    transport = FakeTransport([_http(), _http()])
    sleep = InterruptingSleep()

    code = main(
        ["run-live", "--db", str(database), "--interval-seconds", "5"],
        config=_config(),
        transport=transport,
        sleep=sleep,
    )

    assert code == 0
    assert sleep.calls == [5.0]


@pytest.mark.parametrize("value", ["0", "-2", "soon"])
def test_run_live_rejects_an_invalid_interval(
    tmp_path: Path, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport([])

    code = main(
        ["run-live", "--db", str(tmp_path / "live.db"), "--interval-seconds", value],
        config=_config(),
        transport=transport,
    )

    assert code != 0
    assert transport.requests == []
    capsys.readouterr()


def test_run_live_rejects_an_unknown_argument(capsys: pytest.CaptureFixture[str]) -> None:
    transport = FakeTransport([])

    code = main(["run-live", "--bogus"], config=_config(), transport=transport)

    assert code != 0
    assert transport.requests == []
    capsys.readouterr()
