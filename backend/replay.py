"""Replay importer for exported chat histories (Phase 9 W4; FR-ING-08).

Replay is not a second ingestion path. Every accepted line is normalized into
the existing :class:`backend.ingest.MessageEvent` and written through
:func:`backend.ingest.ingest_message`, so duplicate handling, flags, mention
and reaction evidence and group resolution behave exactly as for live mail.

Owner decision O9-4, approved with an amendment:

- ``sender_map`` maps an export sender *label* (a display name or phone
  number) to a Discord user ID. A non-blank label absent from the map is
  stored with ``sender_discord_user_id = None``; ingestion records it as
  ``UNKNOWN_SENDER`` and the batch counts the distinct labels. It is never
  guessed to a person.
- ``group_map`` maps an export group label to a Discord channel ID, which must
  resolve to an existing ``source_group``. An unmapped label aborts before any
  write; the error names the label, never the content.
- A line without ``source_message_id`` gets
  ``replay:<sha256(group|timestamp|sender label|content)[:32]>``. The four
  inputs are the exported values *before* mapping, so changing a mapping does
  not renumber history; identical exported lines intentionally share an
  identity and the second is a duplicate.

The file is normalized with an explicit ``time_zone`` (default
``Asia/Kolkata``): a naive timestamp is localized before ingestion, so the
stored event time is already aware and PB-2 records ``time_basis='EVENT'``.
The original raw timestamp of every localized line is preserved in the batch
counts (and the original line itself as the message's ``raw_metadata``). An
invalid or blank time zone aborts before any write.

One row in 0021 ``replay_batch`` records ``file_sha256``, time zone, counts
and outcome. Re-importing a completed file re-runs the same ingestion path
(all rows come back as duplicates), reports them, and writes no new batch row
or message. A failed import can be retried and its batch row is completed.
The group clock must be ``REPLAY`` (or unset) and is advanced to the latest
imported event time, so replay is deterministic and independent of wall-clock
speed (PB-1/PB-2).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.clock import advance_group_clock, read_group_clock
from backend.db.connection import transaction
from backend.ingest import (
    MentionEvent,
    MessageEvent,
    ReactionEvent,
    ingest_message,
)

DEFAULT_REPLAY_TIME_ZONE = "Asia/Kolkata"

#: Fixed offsets for zones the system tz database may not carry. Windows has no
#: tz database unless the optional ``tzdata`` package is installed, and the
#: project has no runtime dependencies; both zones listed here have no DST, so a
#: fixed offset is exactly equivalent to the named zone. Any other zone name
#: still resolves through :mod:`zoneinfo` when that database is available and is
#: refused as unknown when it is not.
_FIXED_OFFSET_ZONES: dict[str, timedelta] = {
    "UTC": timedelta(0),
    "Etc/UTC": timedelta(0),
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "Asia/Calcutta": timedelta(hours=5, minutes=30),
}

#: A timestamp with an explicit seconds field is exact enough for FR-ING-08.
_SECONDS = re.compile(r"[T ]\d{2}:\d{2}:\d{2}")


class ReplayError(ValueError):
    """Raised before any write when an export cannot be replayed exactly."""


@dataclass(frozen=True)
class ReplayBatch:
    """The durable outcome of one import (0021) plus this run's counts."""

    batch_id: int | None
    source: str
    file_sha256: str
    time_zone: str
    outcome: str
    counts: Mapping[str, object]
    already_imported: bool = False


@dataclass(frozen=True)
class _PreparedLine:
    """One validated line, ready for the live ingestion path."""

    index: int
    event: MessageEvent
    group_label: str
    sender_label: str | None
    raw_timestamp: str
    localized: bool
    has_seconds: bool
    derived_id: bool


def import_export(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    time_zone: str = DEFAULT_REPLAY_TIME_ZONE,
    source: str = "replay",
    sender_map: Mapping[str, str] | None = None,
    group_map: Mapping[str, str] | None = None,
    batch_metadata: Mapping[str, str] | None = None,
) -> ReplayBatch:
    """Import one JSONL export through ``ingest_message``; see the module doc.

    All structural validation, group resolution, time-zone handling and clock
    mode checks happen before the first write. ``batch_metadata`` is provenance
    supplied by the caller and stored inside ``counts_json`` (the schema's only
    JSON column); it is never treated as message content.
    """
    if source is None or source.strip() == "":
        raise ReplayError("replay source must be a non-blank value")
    zone = _resolve_zone(time_zone)
    mapping = dict(sender_map or {})
    groups = dict(group_map or {})
    export_path = Path(path)
    data = _read_export(export_path)
    file_sha256 = hashlib.sha256(data).hexdigest()
    prepared, skipped_blank = _prepare_lines(
        connection,
        data,
        zone=zone,
        source=source,
        sender_map=mapping,
        group_map=groups,
    )
    group_labels = _group_labels(connection, prepared)
    _require_replay_mode(connection, group_labels)
    counts = _initial_counts(
        prepared,
        skipped_blank=skipped_blank,
        time_zone=time_zone,
        batch_metadata=batch_metadata,
    )
    existing = _existing_batch(connection, file_sha256, time_zone)
    already_imported = existing is not None and str(existing["outcome"]) == "COMPLETED"
    if existing is None:
        batch_id = _start_batch(connection, source, file_sha256, time_zone, counts)
    else:
        batch_id = int(existing["id"])
        if not already_imported:
            _restart_batch(connection, batch_id, counts)

    try:
        for line in prepared:
            result = ingest_message(connection, line.event)
            if result.created:
                _bump(counts, "inserted")
            else:
                _bump(counts, "duplicate")
        _advance_replay_clocks(connection, prepared)
    except BaseException:
        if not already_imported:
            _finish_batch(connection, batch_id, "FAILED", counts)
        raise
    if not already_imported:
        _finish_batch(connection, batch_id, "COMPLETED", counts)
    return ReplayBatch(
        batch_id=batch_id,
        source=source,
        file_sha256=file_sha256,
        time_zone=time_zone,
        outcome="COMPLETED",
        counts=counts,
        already_imported=already_imported,
    )


# --- reading and validation --------------------------------------------------


def _read_export(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ReplayError(f"cannot read replay export: {error}") from error


def _prepare_lines(
    connection: sqlite3.Connection,
    data: bytes,
    *,
    zone: tzinfo,
    source: str,
    sender_map: Mapping[str, str],
    group_map: Mapping[str, str],
) -> tuple[list[_PreparedLine], int]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReplayError("replay export is not valid UTF-8") from error
    group_cache: dict[str, str] = {}
    prepared: list[_PreparedLine] = []
    skipped_blank = 0
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line_text = raw_line.strip()
        if line_text == "":
            skipped_blank += 1
            continue
        try:
            payload = json.loads(line_text)
        except ValueError as error:
            raise ReplayError(f"line {index}: not valid JSON") from error
        if not isinstance(payload, dict):
            raise ReplayError(f"line {index}: expected a JSON object")
        group_label = _require_text(payload, "group", index)
        timestamp = _require_text(payload, "timestamp", index)
        content = _require_text(payload, "content", index)
        sender_label = _optional_text(payload, "sender", index)
        source_message_id = _optional_text(payload, "source_message_id", index)
        reply_to = _optional_text(payload, "reply_to", index)
        channel = _resolve_channel(
            connection, group_label, group_map, group_cache, index
        )
        instant, localized = _parse_timestamp(timestamp, zone, index)
        sender_discord_user_id = (
            None if sender_label is None else sender_map.get(sender_label)
        )
        if source_message_id is None:
            derived = True
            message_id = _derived_message_id(
                group_label, timestamp, sender_label, content
            )
        else:
            derived = False
            message_id = source_message_id
        event = MessageEvent(
            source=source,
            source_message_id=message_id,
            discord_channel_id=channel,
            sender_discord_user_id=sender_discord_user_id,
            event_timestamp=instant.isoformat(),
            received_at=instant.isoformat(),
            content=content,
            reply_reference=reply_to,
            raw_metadata=line_text,
            mentions=_mentions(payload.get("mentions"), index),
            reactions=_reactions(payload.get("reactions"), index),
        )
        prepared.append(
            _PreparedLine(
                index=index,
                event=event,
                group_label=group_label,
                sender_label=sender_label,
                raw_timestamp=timestamp,
                localized=localized,
                has_seconds=bool(_SECONDS.search(timestamp)),
                derived_id=derived,
            )
        )
    return prepared, skipped_blank


def _resolve_zone(time_zone: str | None) -> tzinfo:
    if time_zone is None or str(time_zone).strip() == "":
        raise ReplayError("replay time zone must be non-blank")
    name = str(time_zone).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        offset = _FIXED_OFFSET_ZONES.get(name)
        if offset is not None:
            return timezone(offset, name)
        raise ReplayError(f"unknown replay time zone {name!r}") from None


def _parse_timestamp(
    raw: str, zone: tzinfo, index: int
) -> tuple[datetime, bool]:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ReplayError(f"line {index}: timestamp is not ISO-8601") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone), True
    return parsed, False


def _resolve_channel(
    connection: sqlite3.Connection,
    group_label: str,
    group_map: Mapping[str, str],
    cache: dict[str, str],
    index: int,
) -> str:
    if group_label in cache:
        return cache[group_label]
    channel = group_map.get(group_label)
    if channel is None or str(channel).strip() == "":
        raise ReplayError(
            f"line {index}: replay group {group_label!r} is not mapped"
            " to a Discord channel"
        )
    channel_text = str(channel)
    row = connection.execute(
        "SELECT id FROM source_group WHERE discord_channel_id = ?",
        (channel_text,),
    ).fetchone()
    if row is None:
        raise ReplayError(
            f"line {index}: replay group {group_label!r} maps to a channel"
            " with no source group"
        )
    cache[group_label] = channel_text
    return channel_text


def _derived_message_id(
    group: str, timestamp: str, sender: str | None, content: str
) -> str:
    seed = f"{group}|{timestamp}|{sender or ''}|{content}".encode()
    return "replay:" + hashlib.sha256(seed).hexdigest()[:32]


def _require_text(payload: Mapping[str, Any], field: str, index: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or value.strip() == "":
        raise ReplayError(f"line {index}: {field} must be a non-blank string")
    return value


def _optional_text(
    payload: Mapping[str, Any], field: str, index: int
) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ReplayError(
            f"line {index}: {field}, when present, must be a non-blank string"
        )
    return value


def _mentions(value: object, index: int) -> tuple[MentionEvent, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReplayError(f"line {index}: mentions must be a list")
    mentions: list[MentionEvent] = []
    for item in value:
        if isinstance(item, str):
            if item.strip() == "":
                raise ReplayError(f"line {index}: mention id must be non-blank")
            mentions.append(MentionEvent(item))
            continue
        if not isinstance(item, dict):
            raise ReplayError(
                f"line {index}: every mention must be a string or an object"
            )
        user_id = item.get("discord_user_id", item.get("mentioned_discord_user_id"))
        if not isinstance(user_id, str) or user_id.strip() == "":
            raise ReplayError(
                f"line {index}: mention discord_user_id must be non-blank"
            )
        position = item.get("position")
        if position is not None and (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position < 0
        ):
            raise ReplayError(
                f"line {index}: mention position must be a non-negative integer"
            )
        mentions.append(MentionEvent(user_id, position))
    return tuple(mentions)


def _reactions(value: object, index: int) -> tuple[ReactionEvent, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReplayError(f"line {index}: reactions must be a list")
    reactions: list[ReactionEvent] = []
    for item in value:
        if not isinstance(item, dict):
            raise ReplayError(f"line {index}: every reaction must be an object")
        reactor = item.get("reactor", item.get("reactor_discord_user_id"))
        emoji = item.get("emoji")
        direction = item.get("direction")
        reacted_at = item.get("reacted_at")
        if not isinstance(reactor, str) or reactor.strip() == "":
            raise ReplayError(f"line {index}: reaction reactor must be non-blank")
        if not isinstance(emoji, str) or emoji.strip() == "":
            raise ReplayError(f"line {index}: reaction emoji must be non-blank")
        if (
            not isinstance(direction, str)
            or direction.strip() == ""
            or direction != direction.strip()
        ):
            raise ReplayError(
                f"line {index}: reaction direction must be trimmed and non-blank"
            )
        if reacted_at is not None and (
            not isinstance(reacted_at, str) or reacted_at.strip() == ""
        ):
            raise ReplayError(
                f"line {index}: reaction reacted_at must be non-blank when present"
            )
        reactions.append(ReactionEvent(reactor, emoji, direction, reacted_at))
    return tuple(reactions)


# --- group clock and batch provenance ---------------------------------------


def _group_labels(
    connection: sqlite3.Connection, prepared: list[_PreparedLine]
) -> dict[int, str]:
    """Map each touched group id to the export label that resolved to it."""
    labels: dict[int, str] = {}
    channels: dict[str, str] = {}
    for line in prepared:
        channels.setdefault(line.event.discord_channel_id, line.group_label)
    for channel, label in channels.items():
        row = connection.execute(
            "SELECT id FROM source_group WHERE discord_channel_id = ?",
            (channel,),
        ).fetchone()
        if row is not None:
            labels[int(row["id"])] = label
    return labels


def _require_replay_mode(
    connection: sqlite3.Connection, group_labels: Mapping[int, str]
) -> None:
    """Replay and live data must not mix in one group's clock (PB-1)."""
    for group_id, label in group_labels.items():
        stored = read_group_clock(connection, group_id)
        if stored is not None and stored.mode != "REPLAY":
            raise ReplayError(
                f"replay group {label!r} clock mode is {stored.mode};"
                " replay requires REPLAY or an unset clock"
            )


def _advance_replay_clocks(
    connection: sqlite3.Connection, prepared: list[_PreparedLine]
) -> None:
    latest: dict[int, datetime] = {}
    for line in prepared:
        row = connection.execute(
            "SELECT id FROM source_group WHERE discord_channel_id = ?",
            (line.event.discord_channel_id,),
        ).fetchone()
        if row is None:
            continue
        instant = datetime.fromisoformat(str(line.event.event_timestamp))
        group_id = int(row["id"])
        current = latest.get(group_id)
        if current is None or instant > current:
            latest[group_id] = instant
    for group_id, instant in sorted(latest.items()):
        advance_group_clock(
            connection, group_id, now=instant.isoformat(), mode="REPLAY"
        )


def _initial_counts(
    prepared: list[_PreparedLine],
    *,
    skipped_blank: int,
    time_zone: str,
    batch_metadata: Mapping[str, str] | None,
) -> dict[str, object]:
    labels = sorted(
        {
            line.sender_label
            for line in prepared
            if line.sender_label is not None
            and line.event.sender_discord_user_id is None
        }
    )
    return {
        "lines": len(prepared) + skipped_blank,
        "records": len(prepared),
        "inserted": 0,
        "duplicate": 0,
        "rejected": 0,
        "skipped_blank_lines": skipped_blank,
        "unknown_sender": sum(
            1 for line in prepared if line.event.sender_discord_user_id is None
        ),
        "unmapped_sender_labels": labels,
        "indicative": any(not line.has_seconds for line in prepared),
        "missing_seconds": sum(1 for line in prepared if not line.has_seconds),
        "derived_ids": sum(1 for line in prepared if line.derived_id),
        "mentions": sum(len(line.event.mentions) for line in prepared),
        "reactions": sum(len(line.event.reactions) for line in prepared),
        "time_zone": time_zone,
        "localized_timestamps": {
            line.event.source_message_id: line.raw_timestamp
            for line in prepared
            if line.localized
        },
        "metadata": dict(batch_metadata or {}),
    }


def _bump(counts: dict[str, object], key: str) -> None:
    """Increment one integer counter in the ``counts_json`` payload."""
    current = counts.get(key, 0)
    if not isinstance(current, int):
        raise ReplayError(f"replay counts field {key!r} is not an integer")
    counts[key] = current + 1


def _existing_batch(
    connection: sqlite3.Connection, file_sha256: str, time_zone: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, outcome FROM replay_batch"
        " WHERE file_sha256 = ? AND time_zone = ?",
        (file_sha256, time_zone),
    ).fetchone()


def _start_batch(
    connection: sqlite3.Connection,
    source: str,
    file_sha256: str,
    time_zone: str,
    counts: Mapping[str, object],
) -> int:
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO replay_batch (source, file_sha256, time_zone,"
            " started_at, finished_at, outcome, counts_json)"
            " VALUES (?, ?, ?, ?, NULL, 'RUNNING', ?)",
            (
                source,
                file_sha256,
                time_zone,
                _utc_now(),
                json.dumps(dict(counts), sort_keys=True),
            ),
        )
    return int(cursor.lastrowid or 0)


def _restart_batch(
    connection: sqlite3.Connection, batch_id: int, counts: Mapping[str, object]
) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE replay_batch SET finished_at = NULL, outcome = 'RUNNING',"
            " counts_json = ? WHERE id = ?",
            (json.dumps(dict(counts), sort_keys=True), batch_id),
        )


def _finish_batch(
    connection: sqlite3.Connection,
    batch_id: int,
    outcome: str,
    counts: Mapping[str, object],
) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE replay_batch SET finished_at = ?, outcome = ?, counts_json = ?"
            " WHERE id = ?",
            (
                _utc_now(),
                outcome,
                json.dumps(dict(counts), sort_keys=True),
                batch_id,
            ),
        )


def _utc_now() -> str:
    """Batch bookkeeping timestamp; import timing is not a replay input."""
    return datetime.now(UTC).isoformat()
