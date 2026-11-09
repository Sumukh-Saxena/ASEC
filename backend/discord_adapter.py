"""Discord intake adapter for ASEC (Phase 9 W5, FR-ING-01, EIR-01).

The adapter is one implementation behind :class:`DiscordSource`: a stdlib
``urllib`` REST poller (owner decision O9-1, no new dependency). It turns raw
Discord message payloads into :class:`backend.ingest.MessageEvent` records and
leaves all authority with the backend — it never writes to the database, never
interprets content, and never creates tasks.

Design notes (plan §4.7, §4.9):

- ``after`` is the newest message snowflake already stored for the channel;
  :func:`read_cursor` reconstructs it from ``source_message``, so no cursor
  table and no migration are needed.
- ``429`` honours ``retry-after`` with a bounded sleep; ``5xx`` and transport
  failures retry with exponential backoff up to the configured cap and then
  raise :class:`DiscordAdapterError`. Error text carries the status code and
  channel id only, never the token or a request URL.
- Discord content is untrusted data. It is preserved verbatim (the raw payload
  is kept in ``raw_metadata``), never executed and never logged here.
- Reaction polling is **deferred by the owner**: :meth:`fetch_reactions` is a
  documented ``NotImplementedError`` stub and no poll path calls it. This is a
  pinned known gap, not a silent omission.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.alerts import EMPTY_CONTENT_RULE, Alert, AlertSender
from backend.config import DiscordSettings
from backend.ingest import MentionEvent, MessageEvent, ReactionEvent

SOURCE_NAME = "discord"
DEFAULT_BASE_URL = "https://discord.com/api/v10"


class DiscordAdapterError(Exception):
    """A Discord request failed or returned something unusable after retries."""


@dataclass(frozen=True)
class HttpResponse:
    """The transport result; a 4xx/5xx is returned, not raised."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    """The only network seam; tests inject a fake implementation."""

    def request(
        self, *, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse: ...


class PostTransport(Protocol):
    """Additive POST seam for the OTP DM sender (Phase 11 W5).

    Polling only needs GET, so this stays separate from :class:`Transport`;
    an injected fake implements only the method its caller uses.
    """

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse: ...


class DiscordSource(Protocol):
    """What ingestion needs from a Discord source (plan §4.7)."""

    def fetch_messages(
        self, *, channel_id: str, after: str | None
    ) -> tuple[MessageEvent, ...]: ...

    def fetch_reactions(
        self, *, channel_id: str, message_id: str
    ) -> tuple[ReactionEvent, ...]: ...

    def fetch_latest_page(self, *, channel_id: str) -> tuple[MessageEvent, ...]: ...


@dataclass(frozen=True)
class PollResult:
    """One poll's normalized messages and its operator counts.

    ``latest_page`` is the first (newest) page the poll fetched, so the health
    check can judge the page a LIVE start would see without a second request.
    ``fetched`` is the raw payload item count returned by Discord (W8b A9);
    ``messages`` contains only the entries that passed the system/bot/sticker
    filters, whose counts are reported separately.
    """

    messages: tuple[MessageEvent, ...]
    latest_page: tuple[MessageEvent, ...]
    fetched: int
    empty_content: int
    with_mentions: int
    with_reply: int
    skipped_system: int = 0
    skipped_bot: int = 0
    skipped_sticker: int = 0


@dataclass(frozen=True)
class _PollStats:
    """Raw and filtered counts accumulated across the fetched pages."""

    fetched: int = 0
    skipped_system: int = 0
    skipped_bot: int = 0
    skipped_sticker: int = 0


@dataclass(frozen=True)
class _PageResult:
    """One page's normalized events, its raw/skip counts and its newest raw id.

    ``last_message_id`` is derived from every raw payload item, not only the
    entries that survived filtering, so pagination cannot stop early or repeat
    a page when a page is mostly system/bot/sticker messages.
    """

    events: tuple[MessageEvent, ...]
    stats: _PollStats
    last_message_id: str | None


class UrllibTransport:
    """Default stdlib transport (owner decision O9-1)."""

    def request(
        self, *, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=int(error.code),
                headers=dict(error.headers or {}),
                body=error.read(),
            )

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=int(error.code),
                headers=dict(error.headers or {}),
                body=error.read(),
            )


class DiscordRestSource:
    """Stdlib REST polling implementation of :class:`DiscordSource`."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        settings: DiscordSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] | None = None,
    ) -> None:
        if token.strip() == "":
            raise DiscordAdapterError("Discord bot token is empty")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._transport = UrllibTransport() if transport is None else transport
        self._settings = DiscordSettings() if settings is None else settings
        self._sleep = sleep
        self._now = _utc_now_iso if now is None else now
        self._last_successful_poll_at: str | None = None
        self._latest_page: tuple[MessageEvent, ...] = ()
        self._last_stats = _PollStats()

    def __repr__(self) -> str:
        # The token is deliberately absent: a source object is safe to log.
        return (
            f"DiscordRestSource(base_url={self._base_url!r},"
            f" page_limit={self._settings.page_limit})"
        )

    @property
    def last_successful_poll_at(self) -> str | None:
        """UTC instant of the last completed fetch, or ``None`` before one."""
        return self._last_successful_poll_at

    def fetch_messages(
        self, *, channel_id: str, after: str | None
    ) -> tuple[MessageEvent, ...]:
        """Fetch every new message for ``channel_id`` after ``after``."""
        messages, latest_page, stats = self._download(
            channel_id=channel_id, after=after, paginate=True
        )
        self._latest_page = latest_page
        self._last_stats = stats
        self._last_successful_poll_at = self._now()
        return messages

    def fetch_latest_page(self, *, channel_id: str) -> tuple[MessageEvent, ...]:
        """Fetch exactly one page (newest first) for the LIVE-start health check."""
        _, latest_page, stats = self._download(
            channel_id=channel_id, after=None, paginate=False
        )
        self._latest_page = latest_page
        self._last_stats = stats
        self._last_successful_poll_at = self._now()
        return latest_page

    def fetch_reactions(
        self, *, channel_id: str, message_id: str
    ) -> tuple[ReactionEvent, ...]:
        """Reaction polling is deferred by the owner (pinned W5 known gap)."""
        raise NotImplementedError(
            "Reaction polling is deferred by the owner; Phase 9 W5 collects no"
            " reaction evidence. Reactions already embedded in a message payload"
            " remain untouched."
        )

    def poll(
        self,
        *,
        channel_id: str,
        after: str | None = None,
        sender: AlertSender | None = None,
    ) -> PollResult:
        """Fetch, normalize, count and alert; ingestion stays the caller's job."""
        messages = self.fetch_messages(channel_id=channel_id, after=after)
        return self._summarize(
            messages,
            latest_page=self._latest_page,
            stats=self._last_stats,
            channel_id=channel_id,
            sender=sender,
        )

    def poll_latest_page(
        self, *, channel_id: str, sender: AlertSender | None = None
    ) -> PollResult:
        """Poll exactly one recent page (the manual smoke command's contract)."""
        page = self.fetch_latest_page(channel_id=channel_id)
        return self._summarize(
            page,
            latest_page=page,
            stats=self._last_stats,
            channel_id=channel_id,
            sender=sender,
        )

    def _summarize(
        self,
        messages: tuple[MessageEvent, ...],
        *,
        latest_page: tuple[MessageEvent, ...],
        stats: _PollStats,
        channel_id: str,
        sender: AlertSender | None,
    ) -> PollResult:
        empty = tuple(message for message in messages if is_empty_content(message))
        if sender is not None and empty:
            at = self._last_successful_poll_at or self._now()
            sender.send(
                Alert(
                    rule=EMPTY_CONTENT_RULE,
                    severity="WARNING",
                    observed=len(empty),
                    threshold=1,
                    message=(
                        f"channel {channel_id}: {len(empty)} message(s) with empty"
                        " content, no attachment and no embed"
                    ),
                    at=at,
                )
            )
        return PollResult(
            messages=messages,
            latest_page=latest_page,
            fetched=stats.fetched,
            empty_content=len(empty),
            with_mentions=sum(1 for message in messages if message.mentions),
            with_reply=sum(1 for message in messages if message.reply_reference),
            skipped_system=stats.skipped_system,
            skipped_bot=stats.skipped_bot,
            skipped_sticker=stats.skipped_sticker,
        )

    def _download(
        self, *, channel_id: str, after: str | None, paginate: bool
    ) -> tuple[
        tuple[MessageEvent, ...], tuple[MessageEvent, ...], _PollStats
    ]:
        cursor = after
        collected: list[MessageEvent] = []
        seen: set[str] = set()
        latest_page: tuple[MessageEvent, ...] = ()
        fetched = 0
        skipped_system = 0
        skipped_bot = 0
        skipped_sticker = 0
        page_cap = self._settings.max_pages if paginate else 1
        for page_index in range(page_cap):
            result = self._fetch_page(channel_id=channel_id, after=cursor)
            page = result.events
            fetched += result.stats.fetched
            skipped_system += result.stats.skipped_system
            skipped_bot += result.stats.skipped_bot
            skipped_sticker += result.stats.skipped_sticker
            if page_index == 0:
                latest_page = page
            if result.stats.fetched == 0:
                break
            for event in page:
                if event.source_message_id not in seen:
                    seen.add(event.source_message_id)
                    collected.append(event)
            if not paginate or result.stats.fetched < self._settings.page_limit:
                break
            newest = result.last_message_id
            if newest is None or newest == cursor:
                break
            cursor = newest
        collected.sort(key=lambda event: _snowflake_key(event.source_message_id))
        stats = _PollStats(
            fetched=fetched,
            skipped_system=skipped_system,
            skipped_bot=skipped_bot,
            skipped_sticker=skipped_sticker,
        )
        return tuple(collected), latest_page, stats

    def _fetch_page(
        self, *, channel_id: str, after: str | None
    ) -> _PageResult:
        query = f"limit={self._settings.page_limit}"
        if after is not None:
            query = f"{query}&after={after}"
        url = f"{self._base_url}/channels/{channel_id}/messages?{query}"
        payload = self._request_json(url, channel_id=channel_id)
        if not isinstance(payload, list):
            raise DiscordAdapterError(
                f"Discord returned a non-list message payload for channel {channel_id}"
            )
        events: list[MessageEvent] = []
        raw_ids: list[str] = []
        skipped_system = 0
        skipped_bot = 0
        skipped_sticker = 0
        for item in payload:
            if not isinstance(item, Mapping):
                raise DiscordAdapterError(
                    "Discord returned a malformed message payload for channel"
                    f" {channel_id}"
                )
            item_id = _text(item.get("id"))
            if item_id is not None:
                raw_ids.append(item_id)
            reason = _skip_reason(
                item, allowed_bot_user_ids=self._settings.allowed_bot_user_ids
            )
            if reason == "system":
                skipped_system += 1
                continue
            if reason == "bot":
                skipped_bot += 1
                continue
            if reason == "sticker":
                skipped_sticker += 1
                continue
            events.append(_normalize_message(item, channel_id=channel_id))
        ordered = tuple(
            sorted(events, key=lambda event: _snowflake_key(event.source_message_id))
        )
        newest_raw = (
            None if not raw_ids else max(raw_ids, key=_snowflake_key)
        )
        return _PageResult(
            events=ordered,
            stats=_PollStats(
                fetched=len(payload),
                skipped_system=skipped_system,
                skipped_bot=skipped_bot,
                skipped_sticker=skipped_sticker,
            ),
            last_message_id=newest_raw,
        )

    def _request_json(self, url: str, *, channel_id: str) -> object:
        headers = {
            "Authorization": f"Bot {self._token}",
            "User-Agent": "ASEC/0.1 (Discord REST polling)",
        }
        delay = self._settings.backoff_base_seconds
        for attempt in range(self._settings.max_attempts):
            last_attempt = attempt + 1 >= self._settings.max_attempts
            try:
                response = self._transport.request(
                    url=url,
                    headers=headers,
                    timeout=self._settings.timeout_seconds,
                )
            except OSError:
                if last_attempt:
                    raise DiscordAdapterError(
                        f"Discord transport failure for channel {channel_id}"
                    ) from None
                self._sleep(delay)
                delay = min(delay * 2, self._settings.backoff_max_seconds)
                continue
            if response.status == 429:
                if last_attempt:
                    raise DiscordAdapterError(
                        f"Discord rate limit not cleared for channel {channel_id}"
                        " (status 429)"
                    )
                self._sleep(
                    min(
                        _retry_after(response, fallback=delay),
                        self._settings.backoff_max_seconds,
                    )
                )
                continue
            if response.status >= 500:
                if last_attempt:
                    raise DiscordAdapterError(
                        f"Discord request failed with status {response.status}"
                        f" for channel {channel_id}"
                    )
                self._sleep(delay)
                delay = min(delay * 2, self._settings.backoff_max_seconds)
                continue
            if not 200 <= response.status < 300:
                raise DiscordAdapterError(
                    f"Discord request failed with status {response.status}"
                    f" for channel {channel_id}"
                )
            try:
                return json.loads(response.body.decode("utf-8") or "null")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise DiscordAdapterError(
                    f"Discord returned invalid JSON for channel {channel_id}"
                ) from None
        raise DiscordAdapterError(
            f"Discord request could not be completed for channel {channel_id}"
        )


def read_cursor(
    connection: sqlite3.Connection, *, channel_id: str, source: str = SOURCE_NAME
) -> str | None:
    """Return the newest stored snowflake for ``channel_id``, or ``None``.

    ``source_message_id`` is a Discord snowflake (numeric). Ordering by length
    then text keeps numeric order without depending on SQLite integer width.
    """
    row = connection.execute(
        "SELECT sm.source_message_id FROM source_message AS sm"
        " JOIN source_group AS g ON g.id = sm.group_id"
        " WHERE sm.source = ? AND g.discord_channel_id = ?"
        " ORDER BY length(sm.source_message_id) DESC,"
        " sm.source_message_id DESC LIMIT 1",
        (source, channel_id),
    ).fetchone()
    return None if row is None else str(row[0])


def is_empty_content(event: MessageEvent) -> bool:
    """True when a payload has no content, no attachment and no embed.

    The judgement uses the raw Discord payload kept in ``raw_metadata`` when it
    is available, so ``content == ""`` with an attachment or an embed is not
    empty (owner addition 1).
    """
    payload = _payload_mapping(event.raw_metadata)
    if payload is None:
        return event.content is None
    content = payload.get("content")
    text = content if isinstance(content, str) else ""
    return (
        text.strip() == ""
        and not payload.get("attachments")
        and not payload.get("embeds")
    )


def check_live_readiness(
    page: Sequence[MessageEvent], *, channel_id: str
) -> None:
    """Refuse a LIVE start when the latest page is entirely empty (owner 2).

    A page of empty messages is the signature of a disabled Message Content
    intent; starting LIVE on it would store metadata without any content.
    """
    if page and all(is_empty_content(event) for event in page):
        raise DiscordAdapterError(
            f"channel {channel_id}: every message in the latest page has empty"
            " content, no attachment and no embed; the Discord Message Content"
            " intent is probably disabled"
        )


def check_live_start(source: DiscordSource, *, channel_id: str) -> None:
    """Fetch the latest page and apply :func:`check_live_readiness`."""
    check_live_readiness(source.fetch_latest_page(channel_id=channel_id), channel_id=channel_id)


def _skip_reason(
    payload: Mapping[str, object], *, allowed_bot_user_ids: frozenset[str]
) -> str | None:
    """Classify a payload the adapter must not ingest (W8b A6/A8).

    Order: a system message type is skipped first, then a bot/webhook author
    (unless explicitly allow-listed), then a sticker-only message. The caller
    counts the returned reason; a skipped payload is never normalized, stored
    or alerted.
    """
    message_type = payload.get("type", 0)
    if message_type not in (0, 19):
        return "system"
    author = payload.get("author")
    author_id: str | None = None
    is_bot = False
    if isinstance(author, Mapping):
        author_id = _text(author.get("id"))
        is_bot = author.get("bot") is True
    if payload.get("webhook_id") is not None:
        is_bot = True
    if is_bot and (author_id is None or author_id not in allowed_bot_user_ids):
        return "bot"
    if _is_sticker_only(payload):
        return "sticker"
    return None


def _is_sticker_only(payload: Mapping[str, object]) -> bool:
    """True for an empty message whose only content is one or more stickers."""
    raw_content = payload.get("content")
    text = raw_content if isinstance(raw_content, str) else ""
    stickers = payload.get("sticker_items")
    return (
        text.strip() == ""
        and not payload.get("attachments")
        and not payload.get("embeds")
        and isinstance(stickers, list)
        and len(stickers) > 0
    )


def _normalize_message(
    payload: Mapping[str, object], *, channel_id: str
) -> MessageEvent:
    message_id = _text(payload.get("id"))
    if message_id is None:
        raise DiscordAdapterError(
            f"Discord returned a message without an id for channel {channel_id}"
        )
    author = payload.get("author")
    sender = None
    if isinstance(author, Mapping):
        sender = _text(author.get("id"))
    reference = payload.get("message_reference")
    reply_reference = None
    if isinstance(reference, Mapping):
        reply_reference = _text(reference.get("message_id"))
    raw_content = payload.get("content")
    content = raw_content if isinstance(raw_content, str) and raw_content else None
    return MessageEvent(
        source=SOURCE_NAME,
        source_message_id=message_id,
        discord_channel_id=_text(payload.get("channel_id")) or channel_id,
        sender_discord_user_id=sender,
        event_timestamp=_text(payload.get("timestamp")),
        content=content,
        reply_reference=reply_reference,
        raw_metadata=dict(payload),
        mentions=_mentions(payload.get("mentions")),
    )


def _mentions(value: object) -> tuple[MentionEvent, ...]:
    if not isinstance(value, list):
        return ()
    mentions: list[MentionEvent] = []
    for item in value:
        if isinstance(item, Mapping):
            mention_id = _text(item.get("id"))
            if mention_id is not None:
                mentions.append(MentionEvent(mentioned_discord_user_id=mention_id))
    return tuple(mentions)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip() != "":
        return value
    return None


def _payload_mapping(
    raw_metadata: object,
) -> Mapping[str, object] | None:
    if isinstance(raw_metadata, Mapping):
        return raw_metadata
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _snowflake_key(message_id: str) -> tuple[int, str]:
    try:
        return (int(message_id), message_id)
    except ValueError:
        return (0, message_id)


def _retry_after(response: HttpResponse, *, fallback: float) -> float:
    header = _header(response.headers, "retry-after")
    if header is not None:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if isinstance(body, Mapping):
        value = body.get("retry_after")
        if isinstance(value, (int, float)):
            return max(float(value), 0.0)
    return fallback


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
