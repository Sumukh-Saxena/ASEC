"""W5 adapter tests: normalization, cursor, pagination, retries, redaction.

All network access goes through an injected fake transport, so this suite stays
offline (plan §4.7, §7). Reaction polling is deferred by the owner: the stub is
asserted to raise and to be reached by no poll path.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from backend.__main__ import PRODUCTION_DB_PATH_KEY, main
from backend.alerts import Alert, LogAlertSender
from backend.config import (
    Config,
    ConfigError,
    DiscordSettings,
    load_discord_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.discord_adapter import (
    DiscordAdapterError,
    DiscordRestSource,
    HttpResponse,
    check_live_readiness,
    check_live_start,
    is_empty_content,
    read_cursor,
)
from backend.ingest import MessageEvent, ingest_message

TS = "2026-01-01T00:00:00+00:00"
FAKE_TOKEN = "test-token-123"


# ---------------------------------------------------------------------------
# Doubles


class FakeTransport:
    """Scripted transport: each request consumes the next response or raises."""

    def __init__(self, responses: Sequence[HttpResponse | Exception]) -> None:
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
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSender:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class StaticSource:
    """A source that returns one fixed page for the health check."""

    def __init__(self, page: tuple[MessageEvent, ...]) -> None:
        self.page = page
        self.latest_calls: list[str] = []

    def fetch_messages(
        self, *, channel_id: str, after: str | None
    ) -> tuple[MessageEvent, ...]:
        raise AssertionError("fetch_messages must not be used by the health check")

    def fetch_latest_page(self, *, channel_id: str) -> tuple[MessageEvent, ...]:
        self.latest_calls.append(channel_id)
        return self.page

    def fetch_reactions(
        self, *, channel_id: str, message_id: str
    ) -> tuple[Any, ...]:
        raise NotImplementedError


def _http(
    status: int = 200,
    body: bytes = b"[]",
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(status=status, headers=dict(headers or {}), body=body)


def _payload(
    message_id: str,
    *,
    content: str = "hello",
    channel_id: str = "chan-1",
    user_id: str = "user-1",
    mentions: Sequence[str] = (),
    reply_to: str | None = None,
    attachments: Sequence[str] = (),
    embeds: Sequence[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": message_id,
        "channel_id": channel_id,
        "content": content,
        "timestamp": "2026-01-01T00:00:00.000000+00:00",
        "author": {"id": user_id},
    }
    if mentions:
        payload["mentions"] = [{"id": mention} for mention in mentions]
    if reply_to is not None:
        payload["message_reference"] = {"message_id": reply_to}
    if attachments:
        payload["attachments"] = [{"id": item} for item in attachments]
    if embeds:
        payload["embeds"] = [{"type": "rich"} for _ in embeds]
    return payload


def _json(messages: Sequence[Mapping[str, Any]]) -> bytes:
    import json

    return json.dumps(list(messages)).encode("utf-8")


def _source(
    transport: FakeTransport,
    *,
    settings: DiscordSettings | None = None,
    sleep: RecordingSleep | None = None,
    now: str = TS,
) -> DiscordRestSource:
    return DiscordRestSource(
        token=FAKE_TOKEN,
        transport=transport,
        settings=settings or DiscordSettings(),
        sleep=sleep or RecordingSleep(),
        now=lambda: now,
    )


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "adapter.db")
    apply_migrations(connection)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Normalization


def test_normalizes_a_discord_message_verbatim() -> None:
    transport = FakeTransport(
        [
            _http(
                body=_json(
                    [
                        _payload(
                            "10",
                            content="lunch at noon",
                            mentions=("mention-1", "mention-2"),
                            reply_to="9",
                        )
                    ]
                )
            )
        ]
    )
    source = _source(transport)

    events = source.fetch_messages(channel_id="chan-1", after=None)

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, MessageEvent)
    assert event.source == "discord"
    assert event.source_message_id == "10"
    assert event.discord_channel_id == "chan-1"
    assert event.sender_discord_user_id == "user-1"
    assert event.event_timestamp == "2026-01-01T00:00:00.000000+00:00"
    assert event.received_at is None  # the backend stamps its own receipt time
    assert event.content == "lunch at noon"
    assert event.reply_reference == "9"
    assert [(m.mentioned_discord_user_id, m.position) for m in event.mentions] == [
        ("mention-1", None),
        ("mention-2", None),
    ]
    # Reactions are deferred: the adapter never invents reaction evidence.
    assert event.reactions == ()
    # The raw payload is preserved as source evidence.
    assert isinstance(event.raw_metadata, Mapping)
    assert event.raw_metadata["id"] == "10"


@pytest.mark.parametrize(
    "item",
    ["not-a-mapping", {"content": "no id"}],
)
def test_malformed_message_payloads_fail_loudly(item: object) -> None:
    import json

    transport = FakeTransport(
        [_http(body=json.dumps([item]).encode("utf-8"))]
    )

    with pytest.raises(DiscordAdapterError) as excinfo:
        _source(transport).fetch_messages(channel_id="chan-1", after=None)

    assert "chan-1" in str(excinfo.value)


def test_missing_channel_and_sender_stay_none() -> None:
    payload = _payload("11")
    payload.pop("author")
    payload.pop("channel_id")
    transport = FakeTransport([_http(body=_json([payload]))])

    event = _source(transport).fetch_messages(channel_id="chan-1", after=None)[0]

    assert event.discord_channel_id == "chan-1"
    assert event.sender_discord_user_id is None


# ---------------------------------------------------------------------------
# Cursor from the database


def test_cursor_is_reconstructed_from_the_database(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
    )
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (2, 'chan-2')"
    )
    for group_id, message_id in (
        (1, "9"),
        (1, "10"),
        (2, "999"),
    ):
        conn.execute(
            "INSERT INTO source_message (source, source_message_id, group_id,"
            " received_at, content) VALUES ('discord', ?, ?, ?, 'x')",
            (message_id, group_id, TS),
        )

    assert read_cursor(conn, channel_id="chan-1") == "10"
    assert read_cursor(conn, channel_id="chan-3") is None


def test_poll_uses_the_database_cursor(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
    )
    conn.execute(
        "INSERT INTO source_message (source, source_message_id, group_id,"
        " received_at, content) VALUES ('discord', '10', 1, ?, 'x')",
        (TS,),
    )
    transport = FakeTransport([_http(body=_json([_payload("11")]))])
    source = _source(transport)

    source.poll(channel_id="chan-1", after=read_cursor(conn, channel_id="chan-1"))

    assert "after=10" in transport.requests[0]["url"]


# ---------------------------------------------------------------------------
# Pagination


def test_pagination_follows_the_newest_snowflake() -> None:
    settings = DiscordSettings(page_limit=2)
    transport = FakeTransport(
        [
            _http(body=_json([_payload("2"), _payload("1")])),
            _http(body=_json([_payload("4"), _payload("3")])),
            _http(body=_json([_payload("5")])),
        ]
    )
    source = _source(transport, settings=settings)

    events = source.fetch_messages(channel_id="chan-1", after=None)

    assert [event.source_message_id for event in events] == ["1", "2", "3", "4", "5"]
    assert len(transport.requests) == 3
    assert "limit=2" in transport.requests[0]["url"]
    assert "after=2" in transport.requests[1]["url"]
    assert "after=4" in transport.requests[2]["url"]


def test_pagination_stops_when_the_cursor_does_not_advance() -> None:
    settings = DiscordSettings(page_limit=2, max_pages=5)
    transport = FakeTransport(
        [
            _http(body=_json([_payload("2"), _payload("1")])),
            _http(body=_json([_payload("2"), _payload("1")])),
        ]
    )
    source = _source(transport, settings=settings)

    events = source.fetch_messages(channel_id="chan-1", after=None)

    assert len(events) == 2
    assert len(transport.requests) == 2


def test_pagination_is_bounded_by_the_configured_page_cap() -> None:
    settings = DiscordSettings(page_limit=1, max_pages=2)
    transport = FakeTransport(
        [
            _http(body=_json([_payload("1")])),
            _http(body=_json([_payload("2")])),
            _http(body=_json([_payload("3")])),
        ]
    )
    source = _source(transport, settings=settings)

    events = source.fetch_messages(channel_id="chan-1", after=None)

    assert [event.source_message_id for event in events] == ["1", "2"]
    assert len(transport.requests) == 2


# ---------------------------------------------------------------------------
# Rate limits and retries


def test_429_honours_retry_after_with_a_bounded_sleep() -> None:
    sleep = RecordingSleep()
    transport = FakeTransport(
        [
            _http(status=429, headers={"retry-after": "3"}),
            _http(body=_json([_payload("1")])),
        ]
    )
    source = _source(transport, sleep=sleep)

    events = source.fetch_messages(channel_id="chan-1", after=None)

    assert [event.source_message_id for event in events] == ["1"]
    assert sleep.calls == [3.0]


def test_429_retry_after_is_capped_at_the_configured_maximum() -> None:
    sleep = RecordingSleep()
    transport = FakeTransport(
        [
            _http(status=429, headers={"retry-after": "9999"}),
            _http(body=_json([_payload("1")])),
        ]
    )
    source = _source(
        transport,
        settings=DiscordSettings(backoff_max_seconds=30.0),
        sleep=sleep,
    )

    source.fetch_messages(channel_id="chan-1", after=None)

    assert sleep.calls == [30.0]


def test_5xx_retries_with_exponential_backoff_then_succeeds() -> None:
    sleep = RecordingSleep()
    transport = FakeTransport(
        [
            _http(status=500),
            _http(status=502),
            _http(body=_json([_payload("1")])),
        ]
    )
    source = _source(transport, settings=DiscordSettings(max_attempts=3), sleep=sleep)

    events = source.fetch_messages(channel_id="chan-1", after=None)

    assert [event.source_message_id for event in events] == ["1"]
    assert sleep.calls == [1.0, 2.0]


def test_5xx_exhausts_retries_and_raises_without_leaking_the_token() -> None:
    transport = FakeTransport([_http(status=503), _http(status=503)])
    source = _source(transport, settings=DiscordSettings(max_attempts=2))

    with pytest.raises(DiscordAdapterError) as excinfo:
        source.fetch_messages(channel_id="chan-1", after=None)

    message = str(excinfo.value)
    assert "503" in message
    assert "chan-1" in message
    assert FAKE_TOKEN not in message
    assert FAKE_TOKEN not in repr(source)


def test_network_errors_retry_then_raise_a_safe_adapter_error() -> None:
    transport = FakeTransport([OSError("socket secret-detail"), OSError("again")])
    source = _source(transport, settings=DiscordSettings(max_attempts=2))

    with pytest.raises(DiscordAdapterError) as excinfo:
        source.fetch_messages(channel_id="chan-1", after=None)

    message = str(excinfo.value)
    assert "chan-1" in message
    assert "secret-detail" not in message
    assert FAKE_TOKEN not in message


def test_the_token_travels_only_in_the_authorization_header() -> None:
    transport = FakeTransport([_http(body=b"[]")])
    source = _source(transport)

    source.fetch_messages(channel_id="chan-1", after=None)

    request = transport.requests[0]
    assert request["headers"]["Authorization"] == f"Bot {FAKE_TOKEN}"
    assert FAKE_TOKEN not in request["url"]


# ---------------------------------------------------------------------------
# Reactions are deferred


def test_reaction_polling_is_a_documented_stub() -> None:
    source = _source(FakeTransport([]))

    with pytest.raises(NotImplementedError):
        source.fetch_reactions(channel_id="chan-1", message_id="1")


def test_polling_never_calls_the_reaction_endpoints() -> None:
    transport = FakeTransport([_http(body=_json([_payload("1")]))])
    sender = RecordingSender()

    _source(transport).poll(channel_id="chan-1", after=None, sender=sender)

    assert len(transport.requests) == 1
    assert "/reactions" not in transport.requests[0]["url"]


# ---------------------------------------------------------------------------
# Last successful poll time


def test_last_successful_poll_time_is_exposed() -> None:
    transport = FakeTransport([_http(body=b"[]")])
    source = _source(transport, now=TS)

    assert source.last_successful_poll_at is None
    source.fetch_messages(channel_id="chan-1", after=None)
    assert source.last_successful_poll_at == TS


def test_a_failed_poll_does_not_advance_the_last_successful_time() -> None:
    transport = FakeTransport([_http(status=500), _http(status=500)])
    source = _source(transport, settings=DiscordSettings(max_attempts=2), now=TS)

    with pytest.raises(DiscordAdapterError):
        source.fetch_messages(channel_id="chan-1", after=None)

    assert source.last_successful_poll_at is None


# ---------------------------------------------------------------------------
# Owner addition 1: EMPTY_CONTENT is stored, counted, flagged and alerted


def test_empty_content_is_stored_as_received_and_counted(
    conn: sqlite3.Connection,
) -> None:
    payload = _payload("20", content="")
    transport = FakeTransport([_http(body=_json([payload]))])
    sender = RecordingSender()
    source = _source(transport)

    result = source.poll(channel_id="chan-1", after=None, sender=sender)

    assert result.fetched == 1
    assert result.empty_content == 1
    assert result.latest_page and is_empty_content(result.latest_page[0])
    assert len(sender.alerts) == 1
    assert sender.alerts[0].rule == "EMPTY_CONTENT"
    assert sender.alerts[0].severity == "WARNING"

    stored = ingest_message(conn, result.messages[0])
    assert stored.created is True
    row = conn.execute(
        "SELECT content FROM source_message WHERE source_message_id = '20'"
    ).fetchone()
    assert row is not None
    assert row["content"] is None  # 0002: empty content is stored as NULL


def test_attachments_or_embeds_are_not_empty_content() -> None:
    with_attachment = _payload("21", content="", attachments=("a-1",))
    with_embed = _payload("22", content="", embeds=("e-1",))
    transport = FakeTransport(
        [_http(body=_json([with_attachment, with_embed]))]
    )
    sender = RecordingSender()
    source = _source(transport)

    result = source.poll(channel_id="chan-1", after=None, sender=sender)

    assert result.empty_content == 0
    assert sender.alerts == []
    assert all(message.content is None for message in result.messages)


def test_whitespace_only_content_is_never_treated_as_a_real_message() -> None:
    transport = FakeTransport([_http(body=_json([_payload("23", content="   ")]))])
    sender = RecordingSender()

    result = _source(transport).poll(
        channel_id="chan-1", after=None, sender=sender
    )

    assert result.empty_content == 1
    assert len(sender.alerts) == 1


def test_poll_counts_mentions_and_replies() -> None:
    transport = FakeTransport(
        [
            _http(
                body=_json(
                    [
                        _payload("30", mentions=("m-1", "m-2"), reply_to="29"),
                        _payload("31"),
                    ]
                )
            )
        ]
    )

    result = _source(transport).poll(channel_id="chan-1", after=None)

    assert result.fetched == 2
    assert result.with_mentions == 1
    assert result.with_reply == 1


# ---------------------------------------------------------------------------
# Owner addition 2: a health check refuses an all-empty latest page


def _empty_event(message_id: str) -> MessageEvent:
    return MessageEvent(
        source="discord",
        source_message_id=message_id,
        discord_channel_id="chan-1",
        raw_metadata={
            "id": message_id,
            "content": "",
            "attachments": [],
            "embeds": [],
        },
    )


def _content_event(message_id: str) -> MessageEvent:
    return MessageEvent(
        source="discord",
        source_message_id=message_id,
        discord_channel_id="chan-1",
        content="text",
        raw_metadata={"id": message_id, "content": "text"},
    )


def test_health_check_refuses_an_all_empty_latest_page() -> None:
    page = (_empty_event("40"), _empty_event("41"), _empty_event("42"))

    with pytest.raises(DiscordAdapterError) as excinfo:
        check_live_readiness(page, channel_id="chan-1")

    assert "Message Content" in str(excinfo.value)
    assert "chan-1" in str(excinfo.value)


def test_health_check_accepts_a_page_with_any_content() -> None:
    page = (_empty_event("40"), _content_event("41"))

    check_live_readiness(page, channel_id="chan-1")


def test_health_check_accepts_an_empty_page() -> None:
    check_live_readiness((), channel_id="chan-1")


def test_live_start_fetches_the_latest_page_before_accepting() -> None:
    source = StaticSource((_empty_event("40"), _empty_event("41")))

    with pytest.raises(DiscordAdapterError):
        check_live_start(source, channel_id="chan-1")

    assert source.latest_calls == ["chan-1"]


def test_health_check_treats_attachments_as_content() -> None:
    event = MessageEvent(
        source="discord",
        source_message_id="40",
        discord_channel_id="chan-1",
        raw_metadata={
            "id": "40",
            "content": "",
            "attachments": [{"id": "a-1"}],
            "embeds": [],
        },
    )

    check_live_readiness((event,), channel_id="chan-1")


# ---------------------------------------------------------------------------
# Alerts


def test_log_alert_sender_logs_structured_fields_only(caplog: pytest.LogCaptureFixture) -> None:
    sender = LogAlertSender()

    with caplog.at_level(logging.WARNING, logger="asec.alerts"):
        sender.send(
            Alert(
                rule="EMPTY_CONTENT",
                severity="WARNING",
                observed=1,
                threshold=1,
                message="channel chan-1: empty content, no attachment and no embed",
                at=TS,
            )
        )

    assert "EMPTY_CONTENT" in caplog.text
    assert "chan-1" in caplog.text


# ---------------------------------------------------------------------------
# Validated configuration


def test_discord_settings_defaults() -> None:
    settings = load_discord_settings(Config({}))

    assert settings.page_limit == 100
    assert settings.timeout_seconds > 0
    assert settings.max_attempts >= 1
    assert settings.poll_interval_seconds == 2.0
    assert settings.max_pages >= 1


def test_discord_settings_are_read_from_configuration() -> None:
    settings = load_discord_settings(
        Config(
            {
                "DISCORD_PAGE_LIMIT": "25",
                "DISCORD_TIMEOUT_SECONDS": "4.5",
                "DISCORD_MAX_ATTEMPTS": "2",
                "DISCORD_BACKOFF_BASE_SECONDS": "0.5",
                "DISCORD_BACKOFF_MAX_SECONDS": "5",
                "DISCORD_POLL_INTERVAL_SECONDS": "3",
                "DISCORD_MAX_PAGES": "4",
            }
        )
    )

    assert settings == DiscordSettings(
        page_limit=25,
        timeout_seconds=4.5,
        max_attempts=2,
        backoff_base_seconds=0.5,
        backoff_max_seconds=5.0,
        poll_interval_seconds=3.0,
        max_pages=4,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DISCORD_PAGE_LIMIT", "0"),
        ("DISCORD_PAGE_LIMIT", "101"),
        ("DISCORD_TIMEOUT_SECONDS", "0"),
        ("DISCORD_MAX_ATTEMPTS", "0"),
        ("DISCORD_MAX_PAGES", "0"),
        ("DISCORD_BACKOFF_MAX_SECONDS", "0"),
    ],
)
def test_invalid_discord_settings_fail_loudly(key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        load_discord_settings(Config({key: value}))


# ---------------------------------------------------------------------------
# Owner addition 4: the owner-authorized manual smoke command


def _smoke_db(tmp_path: Path, *, preloaded: Sequence[MessageEvent] = ()) -> Path:
    path = tmp_path / "scratch.db"
    connection = connect(path)
    try:
        apply_migrations(connection)
        for event in preloaded:
            ingest_message(connection, event)
    finally:
        connection.close()
    return path


def _smoke_config(db: Path | None = None) -> Config:
    values = {"DISCORD_BOT_TOKEN": FAKE_TOKEN}
    if db is not None:
        values[PRODUCTION_DB_PATH_KEY] = str(db)
    return Config(values)


def _run_smoke(
    argv: Sequence[str], *, transport: FakeTransport, config: Config | None = None
) -> int:
    return main(
        ["discord-smoke", *argv],
        config=config or Config({"DISCORD_BOT_TOKEN": FAKE_TOKEN}),
        transport=transport,
    )


def test_smoke_command_ingests_one_page_and_prints_only_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    duplicate = MessageEvent(
        source="discord",
        source_message_id="50",
        discord_channel_id="chan-1",
        content="already stored",
    )
    db = _smoke_db(tmp_path, preloaded=(duplicate,))
    transport = FakeTransport(
        [
            _http(
                body=_json(
                    [
                        _payload("50", content="already stored", user_id="user-50"),
                        _payload("51", content="fresh", mentions=("m-1",)),
                        _payload("52", content="", user_id="user-52"),
                    ]
                )
            )
        ]
    )

    code = _run_smoke(
        ["--channel-id", "chan-1", "--db", str(db)], transport=transport
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == (
        "fetched=3 stored=2 duplicates=1"
        " empty_content=1 with_mentions=1 with_reply=0"
    )
    assert "fresh" not in captured.out
    assert "user-50" not in captured.out
    assert "user-52" not in captured.out
    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err


def test_smoke_command_fetches_one_recent_page_without_using_the_cursor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cursor_row = MessageEvent(
        source="discord",
        source_message_id="999",
        discord_channel_id="chan-1",
        content="older stored row",
    )
    db = _smoke_db(tmp_path, preloaded=(cursor_row,))
    transport = FakeTransport(
        [_http(body=_json([_payload("50"), _payload("51")]))]
    )

    code = _run_smoke(
        ["--channel-id", "chan-1", "--db", str(db)], transport=transport
    )

    captured = capsys.readouterr()
    assert code == 0
    assert len(transport.requests) == 1
    assert "after=" not in transport.requests[0]["url"]
    assert captured.out.strip() == (
        "fetched=2 stored=2 duplicates=0"
        " empty_content=0 with_mentions=0 with_reply=0"
    )


def test_smoke_command_defaults_to_an_in_memory_scratch_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = FakeTransport([_http(body=_json([_payload("60")]))])

    code = _run_smoke(["--channel-id", "chan-1"], transport=transport)

    assert code == 0
    assert "fetched=1 stored=1 duplicates=0" in capsys.readouterr().out


def test_smoke_command_refuses_the_production_database_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _smoke_db(tmp_path)
    transport = FakeTransport([_http(body=b"[]")])

    code = _run_smoke(
        ["--channel-id", "chan-1", "--db", str(db)],
        transport=transport,
        config=_smoke_config(db),
    )

    captured = capsys.readouterr()
    assert code != 0
    assert "production" in captured.err.lower()
    assert transport.requests == []


def test_smoke_command_requires_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _smoke_db(tmp_path)
    transport = FakeTransport([_http(body=b"[]")])

    code = _run_smoke(
        ["--channel-id", "chan-1", "--db", str(db)],
        transport=transport,
        config=Config({}),
    )

    captured = capsys.readouterr()
    assert code != 0
    assert "DISCORD_BOT_TOKEN" in captured.err
    assert transport.requests == []


def test_smoke_command_requires_a_channel_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run_smoke([], transport=FakeTransport([]))

    assert code != 0
    capsys.readouterr()
