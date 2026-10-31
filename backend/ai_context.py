"""Shared AI-request context composition (Phase 7 — S1).

One tested implementation of the request-copy rules both AI stages share —
the Phase 4 relevance classifier and the Phase 5 proposal generator — per
EIR-03 (docs/SRS.md §5), NFR-SEC-04, DR-03 and AC-21:

- Stored content is never modified; masking and markup rewriting happen on the
  request copy only.
- ``mask_pii`` runs first, then Discord markup is rewritten, so a maskable
  digit run can never be turned into a handle and no raw Discord id survives.
- Mentions become opaque ``@m1, @m2, …`` handles assigned in first-appearance
  order scanning the bucket by ascending message id: stored mentions first
  (position, then row id), then content mentions. This is exactly the Phase 5
  algorithm, now the single implementation both stages consume.
- Reply references resolve to internal ``source_message.id`` values of the
  same bucket, otherwise ``None`` (DC-19); a raw reply reference never leaves
  the backend.

``reply_to_message_id`` takes the connection because a raw Discord reply
reference can only be turned into an internal id by looking the message up
(``source_message`` is unique per ``(source, source_message_id)``); the plan's
sketch omitted that parameter.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from backend.masking import mask_pii

_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([a-zA-Z0-9_]+):\d+>")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")


class MentionHandles:
    """Opaque per-request handles, assigned in first-appearance order (EIR-03).

    One instance belongs to one request (bucket): the numbering starts at
    ``@m1`` for the first distinct Discord user id it sees and never renumbers.
    The mapping stays on the backend; only the handles leave it.
    """

    def __init__(self) -> None:
        self._handle_by_user: dict[str, str] = {}
        self._user_order: list[str] = []

    def handle(self, discord_user_id: str) -> str:
        """Return this user's handle, assigning the next one on first sight."""
        existing = self._handle_by_user.get(discord_user_id)
        if existing is not None:
            return existing
        handle = f"@m{len(self._user_order) + 1}"
        self._handle_by_user[discord_user_id] = handle
        self._user_order.append(discord_user_id)
        return handle

    @property
    def mapping(self) -> tuple[tuple[str, str], ...]:
        """``(handle, discord_user_id)`` pairs in assignment order."""
        return tuple(
            (self._handle_by_user[user_id], user_id) for user_id in self._user_order
        )

    def handles_in(self, content: str) -> tuple[str, ...]:
        """Handles for the user mentions in ``content``, first-appearance order.

        Assigns unseen users (so calling it before or after
        :func:`rewrite_markup` yields the same numbering) and de-duplicates
        repeated mentions of the same user within one message.
        """
        handles: list[str] = []
        for match in _USER_MENTION_RE.finditer(content):
            handle = self.handle(match.group(1))
            if handle not in handles:
                handles.append(handle)
        return tuple(handles)


def rewrite_markup(content: str, handles: MentionHandles) -> str:
    """Rewrite Discord markup in an already-masked request copy (EIR-03).

    User mentions (``<@ID>``, ``<@!ID>``) become opaque handles; role, channel
    and custom-emoji forms become ``@role``, ``#channel`` and ``:emoji:`` with
    no identifier. A user mention is always mapped: there is no pass-through
    path for a raw id (plan §7 mutation 9).
    """
    rewritten = _USER_MENTION_RE.sub(
        lambda match: handles.handle(match.group(1)), content
    )
    rewritten = _ROLE_MENTION_RE.sub("@role", rewritten)
    rewritten = _CHANNEL_MENTION_RE.sub("#channel", rewritten)
    rewritten = _CUSTOM_EMOJI_RE.sub(r":\1:", rewritten)
    return rewritten


def _masked_copy(
    content: str | None, handles: MentionHandles
) -> tuple[str | None, tuple[str, ...]]:
    """Mask then rewrite once; also return the content's mention handles."""
    if content is None:
        return None, ()
    masked = mask_pii(content)
    rewritten = rewrite_markup(masked, handles)
    return rewritten, handles.handles_in(masked)


def masked_request_content(
    content: str | None, handles: MentionHandles
) -> str | None:
    """The adapter-facing content copy: mask first, rewrite second (rule 1).

    ``None`` stays ``None`` (missing stored as missing, DR-04). The stored
    ``source_message.content`` is never touched (DR-03, AC-21).
    """
    return _masked_copy(content, handles)[0]


def reply_to_message_id(
    connection: sqlite3.Connection,
    raw_reply: str | None,
    *,
    group_id: int,
    bucket_message_ids: frozenset[int],
) -> int | None:
    """Resolve a stored reply reference to an internal id of this bucket.

    The lookup is scoped to the message's group; the result is returned only
    when the referenced message is a member of ``bucket_message_ids``.
    Anything else — unknown, external, or a different bucket — is ``None``
    (DC-19), never a guessed or cross-bucket link.
    """
    if raw_reply is None:
        return None
    row = connection.execute(
        "SELECT id FROM source_message"
        " WHERE group_id = ? AND source_message_id = ?",
        (group_id, str(raw_reply)),
    ).fetchone()
    if row is None:
        return None
    message_id = int(row["id"])
    return message_id if message_id in bucket_message_ids else None


@dataclass(frozen=True)
class BucketMessageEntry:
    """One bucket message as both AI stages see it (EIR-03 request copy).

    ``masked_content`` is masked and markup-rewritten; ``mention_handles``
    holds the opaque handles for the message's stored mentions followed by its
    content mentions. ``reply_to_message_id`` is an internal id or ``None``.
    """

    message_id: int
    sender_person_id: int | None
    sender_name: str | None
    event_timestamp: str | None
    reply_to_message_id: int | None
    masked_content: str | None
    mention_handles: tuple[str, ...]


def bucket_message_entries(
    connection: sqlite3.Connection, bucket_id: int
) -> tuple[tuple[BucketMessageEntry, ...], tuple[tuple[str, str], ...]]:
    """Build every bucket message's request copy and the handle mapping.

    Messages are ordered by ascending id. Handles are assigned across the
    whole bucket before content rewriting: stored mentions first (message id,
    then position, then row id), exactly the Phase 5 algorithm, so both stages
    number identical buckets identically.
    """
    handles = MentionHandles()
    handles_by_message: dict[int, list[str]] = {}
    for row in connection.execute(
        "SELECT bm2.message_id AS message_id,"
        "       mm.mentioned_discord_user_id AS user_id,"
        "       mm.position AS position, mm.id AS mention_row_id"
        "  FROM message_mention AS mm"
        "  JOIN bucket_message AS bm2 ON bm2.message_id = mm.message_id"
        " WHERE bm2.bucket_id = ?"
        " ORDER BY mm.message_id,"
        "          (mm.position IS NULL), mm.position, mm.id",
        (bucket_id,),
    ).fetchall():
        handle = handles.handle(str(row["user_id"]))
        handles_by_message.setdefault(int(row["message_id"]), []).append(handle)

    rows = connection.execute(
        "SELECT m.id, m.group_id, m.source_message_id, m.sender_person_id,"
        "       p.name AS sender_name, m.event_timestamp,"
        "       m.reply_reference, m.content"
        "  FROM bucket_message AS bm"
        "  JOIN source_message AS m ON m.id = bm.message_id"
        "  LEFT JOIN person AS p ON p.id = m.sender_person_id"
        " WHERE bm.bucket_id = ?"
        " ORDER BY m.id",
        (bucket_id,),
    ).fetchall()
    bucket_ids = frozenset(int(row["id"]) for row in rows)

    entries: list[BucketMessageEntry] = []
    for row in rows:
        message_id = int(row["id"])
        content_copy, content_handles = _masked_copy(row["content"], handles)
        message_handles = list(handles_by_message.get(message_id, ()))
        for handle in content_handles:
            if handle not in message_handles:
                message_handles.append(handle)
        entries.append(
            BucketMessageEntry(
                message_id=message_id,
                sender_person_id=(
                    None
                    if row["sender_person_id"] is None
                    else int(row["sender_person_id"])
                ),
                sender_name=(
                    None if row["sender_name"] is None else str(row["sender_name"])
                ),
                event_timestamp=(
                    None
                    if row["event_timestamp"] is None
                    else str(row["event_timestamp"])
                ),
                reply_to_message_id=reply_to_message_id(
                    connection,
                    (
                        None
                        if row["reply_reference"] is None
                        else str(row["reply_reference"])
                    ),
                    group_id=int(row["group_id"]),
                    bucket_message_ids=bucket_ids,
                ),
                masked_content=content_copy,
                mention_handles=tuple(message_handles),
            )
        )
    return tuple(entries), handles.mapping
