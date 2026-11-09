"""Notification rendering, in-app list, read state and push endpoints.

Phase 11 W1 (plan §4.1): the notification row stores the event and its subject,
not a frozen sentence. Rendering composes the body from the current domain
state, so wording cannot drift from reality (FR-NOT-03). The in-app list is
self-only (NFR-SEC-03) and is the guaranteed fallback channel for rows whose
delivery did not succeed.

Tokens are secrets. They are accepted by :func:`register_endpoint`, read only
to hand to a sender, and never placed in a log line, an exception message or a
``repr`` (NFR-SEC-01).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from backend.authorization import AuthorizationError, record_denial
from backend.config import DISCORD_BOT_TOKEN_KEY, Config, NotificationSettings
from backend.db.connection import transaction
from backend.discord_adapter import DEFAULT_BASE_URL, PostTransport
from backend.notification_events import (
    EVENT_ANNOUNCEMENT,
    EVENT_ASSIGNEE_ACCEPTED,
    EVENT_ASSIGNEE_DENIED,
    EVENT_CROSS_VERTICAL_DECISION,
    EVENT_CROSS_VERTICAL_REQUEST,
    EVENT_HEAD_CROSS_VERTICAL_TASK,
    EVENT_OPEN_TASK_AVAILABLE,
    EVENT_PROPOSAL_READY,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_EDITED,
    EVENT_TASK_REASSIGNED,
    EVENT_WORK_COMPLETED,
    announcement_key,
    stage_notification,
)

__all__ = [
    "AnnouncementNotFoundError",
    "DeliveryBatch",
    "DevelopmentOtpSender",
    "DiscordDmOtpSender",
    "NotificationAuthorizationError",
    "NotificationError",
    "NotificationNotFoundError",
    "NotificationRenderError",
    "NotificationView",
    "OtpSender",
    "OtpSenderNotConfigured",
    "PushDeliveryError",
    "PushEndpointConflict",
    "PushSender",
    "RecordingOtpSender",
    "deactivate_endpoint",
    "deliver_pending",
    "fan_out_announcement",
    "list_notifications",
    "mark_read",
    "register_endpoint",
    "render_notification",
    "require_otp_sender",
    "run_delivery_tick",
    "sweep_unfanned_announcements",
]

logger = logging.getLogger("asec.notification_delivery")


class NotificationError(Exception):
    """Base class for notification-domain errors."""


class NotificationNotFoundError(NotificationError):
    """The notification row does not exist."""

    def __init__(self, notification_id: int) -> None:
        self.notification_id = notification_id
        super().__init__(f"notification {notification_id} not found")


class AnnouncementNotFoundError(NotificationError):
    """The announcement row does not exist."""

    def __init__(self, announcement_id: int) -> None:
        self.announcement_id = announcement_id
        super().__init__(f"announcement {announcement_id} not found")


class NotificationRenderError(NotificationError):
    """The notification cannot be rendered without inventing state."""


class NotificationAuthorizationError(AuthorizationError, NotificationError):
    """The caller tried to read or mark another person's notification."""


class PushEndpointConflict(NotificationError):
    """A token already belongs to another person (the token is never echoed)."""


class PushSender(Protocol):
    """The provider boundary: the worker never imports a concrete sender."""

    def send(
        self,
        *,
        token: str,
        title: str,
        body: str,
        deep_link: str | None,
        collapse_key: str,
    ) -> str | None: ...


class PushDeliveryError(Exception):
    """A sanitized provider failure (never carries a token, phone or body)."""

    def __init__(
        self,
        *,
        permanent: bool,
        status_code: int | None = None,
        provider_code: str | None = None,
    ) -> None:
        self.permanent = permanent
        self.status_code = status_code
        self.provider_code = provider_code
        super().__init__(_sanitized_push_error(self))


class OtpSender(Protocol):
    """The Phase 12 boundary: a code goes to the roster Discord id only."""

    def send(self, *, discord_id: str, code: str) -> None: ...


class OtpSenderNotConfigured(NotificationError):
    """No OTP sender is configured for a production sign-in path."""


class RecordingOtpSender:
    """Test double: records the code in memory and never sends it."""

    def __init__(self) -> None:
        self._sent: list[tuple[str, str]] = []

    def send(self, *, discord_id: str, code: str) -> None:
        self._sent.append((discord_id, code))

    @property
    def sent(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._sent)

    def __repr__(self) -> str:
        # The code and Discord id are deliberately absent.
        return f"RecordingOtpSender(sent={len(self._sent)})"


class DevelopmentOtpSender(RecordingOtpSender):
    """Local-run sender: records the code, refuses production use (O11-1)."""

    def __init__(self, *, production: bool = False) -> None:
        super().__init__()
        self._production = production

    def send(self, *, discord_id: str, code: str) -> None:
        if self._production:
            raise OtpSenderNotConfigured(
                "development OTP sender is disabled in production"
            )
        super().send(discord_id=discord_id, code=code)

    def __repr__(self) -> str:
        return (
            f"DevelopmentOtpSender(production={self._production},"
            f" sent={len(self.sent)})"
        )


def _json_object(body: bytes) -> dict[str, object] | None:
    """Parse a provider body without ever putting it in an error or a log."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class DiscordDmOtpSender:
    """Open a DM with the roster Discord id and send one OTP message (O11-1).

    The bot token is read only through :meth:`Config.require`. A failed DM is
    recorded as a sanitised outcome and returns ``None`` exactly like a
    delivered one, so the sign-in response cannot reveal whether it arrived.
    The OTP code is in the request body only; it never enters a log line, an
    outcome, an exception or a ``repr``.
    """

    def __init__(
        self,
        *,
        config: Config,
        transport: PostTransport,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._token = config.require(DISCORD_BOT_TOKEN_KEY)
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._outcomes: list[str] = []

    @property
    def outcomes(self) -> tuple[str, ...]:
        return tuple(self._outcomes)

    def __repr__(self) -> str:
        # The token is deliberately absent: the sender is safe to log.
        return f"DiscordDmOtpSender(base_url={self._base_url!r})"

    def send(self, *, discord_id: str, code: str) -> None:
        if discord_id.strip() == "" or code.strip() == "":
            raise ValueError("discord_id and code must not be blank")
        outcome = self._deliver(discord_id=discord_id, code=code)
        self._outcomes.append(outcome)
        if outcome != "SENT":
            # Sanitised: no code, token, Discord id or provider body.
            logger.warning("OTP DM delivery failed: %s", outcome)

    def _deliver(self, *, discord_id: str, code: str) -> str:
        headers = {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
            "User-Agent": "ASEC/0.1 (OTP DM)",
        }
        try:
            channel_response = self._transport.post(
                url=f"{self._base_url}/users/@me/channels",
                headers=headers,
                body=json.dumps({"recipient_id": discord_id}).encode("utf-8"),
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001 - a failed DM must not raise
            return "TRANSPORT_ERROR"
        if not 200 <= channel_response.status < 300:
            return f"HTTP {channel_response.status}"
        channel = _json_object(channel_response.body)
        channel_id = None if channel is None else channel.get("id")
        if (
            not isinstance(channel_id, str)
            or not channel_id.isascii()
            or not channel_id.isdigit()
        ):
            return "MALFORMED_RESPONSE"
        try:
            message_response = self._transport.post(
                url=f"{self._base_url}/channels/{channel_id}/messages",
                headers=headers,
                body=json.dumps(
                    {"content": f"Your ASEC sign-in code is {code}"}
                ).encode("utf-8"),
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001 - a failed DM must not raise
            return "TRANSPORT_ERROR"
        if not 200 <= message_response.status < 300:
            return f"HTTP {message_response.status}"
        message = _json_object(message_response.body)
        message_id = None if message is None else message.get("id")
        if not isinstance(message_id, str) or message_id.strip() == "":
            return "MALFORMED_RESPONSE"
        return "SENT"


def require_otp_sender(otp_sender: OtpSender | None) -> OtpSender:
    """Return the configured sender or raise the explicit unconfigured error."""
    if otp_sender is None:
        raise OtpSenderNotConfigured("OTP sender is not configured")
    return otp_sender


@dataclass(frozen=True)
class DeliveryBatch:
    """The real outcome counts of one :func:`deliver_pending` call."""

    attempted: int
    sent: int
    retrying: int
    exhausted: int


@dataclass(frozen=True)
class NotificationView:
    """One notification rendered from the current domain state."""

    notification_id: int
    event_type: str
    title: str
    body: str
    deep_link: str | None
    created_at: str
    read_at: str | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _notification_row(
    connection: sqlite3.Connection, notification_id: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT n.id, n.event_type, n.recipient_person_id, n.proposal_id,"
        " n.task_id, n.approval_request_id, n.announcement_id,"
        " n.delivery_state, n.created_at, r.read_at"
        " FROM notification AS n"
        " LEFT JOIN notification_read AS r ON r.notification_id = n.id"
        " WHERE n.id = ?",
        (notification_id,),
    ).fetchone()
    if row is None:
        raise NotificationNotFoundError(notification_id)
    return row


def _subject_row(
    connection: sqlite3.Connection, table: str, row_id: int
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        raise NotificationRenderError(f"notification subject {table}:{row_id} is missing")
    return row


def _proposal_summary(connection: sqlite3.Connection, proposal_id: int) -> str:
    proposal = _subject_row(connection, "proposal", proposal_id)
    description = proposal["description"]
    return "No description recorded." if description is None else str(description)


def _task_row(connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    return _subject_row(connection, "task", task_id)


def _task_description(task: sqlite3.Row) -> str:
    return str(task["description"])


def _person_name(connection: sqlite3.Connection, person_id: int) -> str:
    row = connection.execute(
        "SELECT name FROM person WHERE id = ?", (person_id,)
    ).fetchone()
    if row is None:
        raise NotificationRenderError(f"person {person_id} is missing")
    return str(row["name"])


def _agreement(
    connection: sqlite3.Connection, task_id: int, person_id: int
) -> str:
    row = connection.execute(
        "SELECT agreement FROM task_assignee"
        " WHERE task_id = ? AND person_id = ? AND cancelled = 0"
        " ORDER BY id DESC LIMIT 1",
        (task_id, person_id),
    ).fetchone()
    return "AWAITING" if row is None else str(row["agreement"])


def _assignment_actor_name(
    connection: sqlite3.Connection,
    task_id: int,
    agreement: str,
    fallback_person_id: int,
) -> str:
    """Name the assignee who acted; the notification recipient is the creator."""
    row = connection.execute(
        "SELECT person_id FROM task_assignee"
        " WHERE task_id = ? AND agreement = ?"
        " ORDER BY agreed_at DESC, id DESC LIMIT 1",
        (task_id, agreement),
    ).fetchone()
    person_id = fallback_person_id if row is None else int(row["person_id"])
    return _person_name(connection, person_id)


def _assignment_body(
    description: str,
    agreement: str,
    *,
    prefix: str = "Task assigned to you",
) -> str:
    if agreement == "ACCEPTED":
        return f"You accepted this task: {description}"
    if agreement == "DENIED":
        return f"You denied this task: {description}"
    return f"{prefix}, awaiting your response: {description}"


def _render_event(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[str, str, str | None]:
    """Return ``(title, body, deep_link)`` for one notification row."""
    event_type = str(row["event_type"])
    recipient_id = int(row["recipient_person_id"])

    if event_type == EVENT_PROPOSAL_READY:
        proposal_id = int(row["proposal_id"])
        return (
            "Proposal ready for review",
            f"Proposal ready for review: {_proposal_summary(connection, proposal_id)}",
            f"/review/{proposal_id}",
        )

    if event_type in (EVENT_TASK_ASSIGNED, EVENT_TASK_REASSIGNED):
        task = _task_row(connection, int(row["task_id"]))
        agreement = _agreement(connection, int(task["id"]), recipient_id)
        prefix = (
            "Task reassigned to you"
            if event_type == EVENT_TASK_REASSIGNED
            else "Task assigned to you"
        )
        title = "Task reassigned" if event_type == EVENT_TASK_REASSIGNED else "Task assigned"
        return (
            title,
            _assignment_body(_task_description(task), agreement, prefix=prefix),
            None,
        )

    if event_type in (EVENT_HEAD_CROSS_VERTICAL_TASK,):
        task = _task_row(connection, int(row["task_id"]))
        agreement = _agreement(connection, int(task["id"]), recipient_id)
        return (
            "Cross-vertical task assigned",
            _assignment_body(
                _task_description(task),
                agreement,
                prefix="Cross-vertical task assigned to you",
            ),
            None,
        )

    if event_type == EVENT_ASSIGNEE_ACCEPTED:
        task = _task_row(connection, int(row["task_id"]))
        name = _assignment_actor_name(
            connection, int(task["id"]), "ACCEPTED", recipient_id
        )
        return (
            "Task accepted",
            f"{name} accepted the task: {_task_description(task)}",
            None,
        )

    if event_type == EVENT_ASSIGNEE_DENIED:
        task = _task_row(connection, int(row["task_id"]))
        name = _assignment_actor_name(
            connection, int(task["id"]), "DENIED", recipient_id
        )
        return (
            "Task denied",
            f"{name} denied the task: {_task_description(task)}",
            None,
        )

    if event_type == EVENT_WORK_COMPLETED:
        task = _task_row(connection, int(row["task_id"]))
        return (
            "Work completed",
            f"Work completed: {_task_description(task)}",
            None,
        )

    if event_type == EVENT_TASK_EDITED:
        task = _task_row(connection, int(row["task_id"]))
        return (
            "Task updated",
            f"Task updated: {_task_description(task)}",
            None,
        )

    if event_type == EVENT_TASK_CANCELLED:
        task = _task_row(connection, int(row["task_id"]))
        return (
            "Task cancelled",
            f"Task cancelled: {_task_description(task)}",
            None,
        )

    if event_type == EVENT_OPEN_TASK_AVAILABLE:
        task = _task_row(connection, int(row["task_id"]))
        return (
            "A place is open",
            f"A place is open on this task: {_task_description(task)}",
            None,
        )

    if event_type == EVENT_CROSS_VERTICAL_REQUEST:
        request = _subject_row(
            connection, "approval_request", int(row["approval_request_id"])
        )
        task = _task_row(connection, int(request["task_id"]))
        message = request["request_message"]
        detail = _task_description(task) if message is None else str(message)
        return (
            "Cross-vertical approval requested",
            f"Cross-vertical approval requested: {detail}",
            None,
        )

    if event_type == EVENT_CROSS_VERTICAL_DECISION:
        request = _subject_row(
            connection, "approval_request", int(row["approval_request_id"])
        )
        decision = connection.execute(
            "SELECT decision, decision_message FROM approval_decision"
            " WHERE request_id = ?",
            (int(request["id"]),),
        ).fetchone()
        if decision is None:
            raise NotificationRenderError(
                f"approval request {int(request['id'])} has no decision"
            )
        word = "approved" if str(decision["decision"]) == "APPROVED" else "denied"
        body = f"Your cross-vertical request was {word}."
        message = decision["decision_message"]
        if message is not None:
            body = f"{body} Decision message: {message}"
        return ("Cross-vertical decision", body, None)

    if event_type == EVENT_ANNOUNCEMENT:
        announcement = _subject_row(
            connection, "announcement", int(row["announcement_id"])
        )
        proposal = _subject_row(
            connection, "proposal", int(announcement["proposal_id"])
        )
        description = proposal["description"]
        if description is None:
            raise NotificationRenderError(
                f"announcement {int(announcement['id'])} has no accepted description"
            )
        return ("Announcement", str(description), None)

    raise NotificationRenderError(f"no renderer for event type {event_type!r}")


def render_notification(
    connection: sqlite3.Connection, notification_id: int
) -> NotificationView:
    """Render one notification from the current state (FR-NOT-03)."""
    row = _notification_row(connection, notification_id)
    title, body, deep_link = _render_event(connection, row)
    return NotificationView(
        notification_id=int(row["id"]),
        event_type=str(row["event_type"]),
        title=title,
        body=body,
        deep_link=deep_link,
        created_at=str(row["created_at"]),
        read_at=None if row["read_at"] is None else str(row["read_at"]),
    )


def list_notifications(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    unread_only: bool = False,
) -> tuple[NotificationView, ...]:
    """List only ``person_id``'s notifications, newest last (NFR-SEC-03)."""
    sql = (
        "SELECT n.id FROM notification AS n"
        " LEFT JOIN notification_read AS r ON r.notification_id = n.id"
        " WHERE n.recipient_person_id = ?"
    )
    parameters: list[object] = [person_id]
    if unread_only:
        sql += " AND r.read_at IS NULL"
    sql += " ORDER BY n.created_at, n.id"
    rows = connection.execute(sql, parameters).fetchall()
    return tuple(render_notification(connection, int(row["id"])) for row in rows)


def mark_read(
    connection: sqlite3.Connection, notification_id: int, *, person_id: int
) -> bool:
    """Mark the caller's own notification read; return False when already read.

    A notification owned by another person is an authorization denial recorded
    through the shared Phase 7 recorder, never a silent no-op (FR-AUT-06).
    Read state never changes delivery state.
    """
    row = connection.execute(
        "SELECT recipient_person_id FROM notification WHERE id = ?",
        (notification_id,),
    ).fetchone()
    if row is None:
        raise NotificationNotFoundError(notification_id)
    if int(row["recipient_person_id"]) != person_id:
        record_denial(
            connection,
            actor_person_id=person_id,
            operation="NOTIFICATION_READ",
            subject_type="NOTIFICATION",
            subject_id=notification_id,
            reason="NOT_RECIPIENT",
        )
        raise NotificationAuthorizationError("NOT_RECIPIENT")

    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO notification_read (notification_id, read_at) VALUES (?, ?)"
            " ON CONFLICT (notification_id) DO NOTHING",
            (notification_id, _utc_now()),
        )
        return cursor.rowcount == 1


def register_endpoint(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    token: str,
    provider: str = "FCM",
    now: str | None = None,
) -> int:
    """Register an active push endpoint and return its id.

    A rotated token deactivates the person's previous active rows; a token that
    already exists is refreshed in place. The token never appears in an error.
    """
    if not token.strip():
        raise ValueError("push endpoint token must not be blank")
    if provider != "FCM":
        raise ValueError("unsupported push provider")
    seen_at = _utc_now() if now is None else now

    with transaction(connection):
        existing = connection.execute(
            "SELECT id, person_id FROM push_endpoint WHERE token = ?", (token,)
        ).fetchone()
        if existing is not None:
            if int(existing["person_id"]) != person_id:
                raise PushEndpointConflict("token is already registered")
            endpoint_id = int(existing["id"])
            connection.execute(
                "UPDATE push_endpoint SET active = 1, last_seen_at = ? WHERE id = ?",
                (seen_at, endpoint_id),
            )
            connection.execute(
                "UPDATE push_endpoint SET active = 0"
                " WHERE person_id = ? AND id != ? AND active = 1",
                (person_id, endpoint_id),
            )
            return endpoint_id

        connection.execute(
            "UPDATE push_endpoint SET active = 0 WHERE person_id = ? AND active = 1",
            (person_id,),
        )
        cursor = connection.execute(
            "INSERT INTO push_endpoint (person_id, provider, token, active,"
            " created_at, last_seen_at) VALUES (?, ?, ?, 1, ?, ?)",
            (person_id, provider, token, seen_at, seen_at),
        )
        return int(cursor.lastrowid or 0)


def deactivate_endpoint(
    connection: sqlite3.Connection, endpoint_id: int, *, person_id: int
) -> bool:
    """Deactivate the caller's own endpoint; return False when not active."""
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE push_endpoint SET active = 0"
            " WHERE id = ? AND person_id = ? AND active = 1",
            (endpoint_id, person_id),
        )
        return cursor.rowcount == 1


# --- delivery worker (W2; FR-NOT-04/05, AC-17) -----------------------------


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _next_attempt_number(
    connection: sqlite3.Connection, notification_id: int
) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) AS last FROM notification_attempt"
        " WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()
    last = 0 if row is None else int(row["last"])
    return last + 1


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    notification_id: int,
    attempt_number: int,
    outcome: str,
    error: str | None,
    recorded_at: str,
) -> None:
    connection.execute(
        "INSERT INTO notification_attempt (notification_id, attempt_number,"
        " outcome, error, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (notification_id, attempt_number, outcome, error, recorded_at),
    )


def _set_delivery_state(
    connection: sqlite3.Connection, notification_id: int, state: str
) -> None:
    connection.execute(
        "UPDATE notification SET delivery_state = ? WHERE id = ?",
        (state, notification_id),
    )


def _recover_sending(
    connection: sqlite3.Connection, *, now: str, settings: NotificationSettings
) -> None:
    """Close claims left behind by a worker that died between send and update.

    The lost send is recorded as a failed attempt, so the duplicate is visible
    in the attempt ledger (the plan's at-least-once caveat).
    """
    rows = connection.execute(
        "SELECT id FROM notification WHERE delivery_state = 'SENDING' ORDER BY id"
    ).fetchall()
    for row in rows:
        notification_id = int(row["id"])
        attempt_number = _next_attempt_number(connection, notification_id)
        _insert_attempt(
            connection,
            notification_id=notification_id,
            attempt_number=attempt_number,
            outcome="FAILED",
            error="WORKER_CRASH_AFTER_SEND",
            recorded_at=now,
        )
        state = (
            "EXHAUSTED"
            if attempt_number >= settings.max_attempts
            else "PENDING"
        )
        _set_delivery_state(connection, notification_id, state)


def _backoff_elapsed(
    connection: sqlite3.Connection,
    notification_id: int,
    *,
    now: str,
    settings: NotificationSettings,
) -> bool:
    row = connection.execute(
        "SELECT attempt_number, recorded_at FROM notification_attempt"
        " WHERE notification_id = ? ORDER BY attempt_number DESC LIMIT 1",
        (notification_id,),
    ).fetchone()
    if row is None:
        return True
    exponent = int(row["attempt_number"]) - 1
    delay = min(
        settings.backoff_base_seconds * (2**exponent),
        settings.backoff_max_seconds,
    )
    eligible_at = _parse_time(str(row["recorded_at"])) + timedelta(seconds=delay)
    return _parse_time(now) >= eligible_at


def _claim(
    connection: sqlite3.Connection,
    *,
    limit: int,
    now: str,
    settings: NotificationSettings,
) -> list[tuple[int, int]]:
    """Select, backoff-filter and claim rows in one BEGIN IMMEDIATE transaction."""
    with transaction(connection):
        _recover_sending(connection, now=now, settings=settings)
        rows = connection.execute(
            "SELECT id, delivery_state FROM notification"
            " WHERE delivery_state IN ('PENDING', 'FAILED')"
            " ORDER BY created_at, id"
        ).fetchall()
        claimed: list[tuple[int, int]] = []
        for row in rows:
            if len(claimed) >= limit:
                break
            notification_id = int(row["id"])
            if str(row["delivery_state"]) == "FAILED" and not _backoff_elapsed(
                connection, notification_id, now=now, settings=settings
            ):
                continue
            cursor = connection.execute(
                "UPDATE notification SET delivery_state = 'SENDING'"
                " WHERE id = ? AND delivery_state IN ('PENDING', 'FAILED')",
                (notification_id,),
            )
            if cursor.rowcount != 1:
                continue
            claimed.append(
                (notification_id, _next_attempt_number(connection, notification_id))
            )
        return claimed


def _active_endpoint_token(
    connection: sqlite3.Connection, person_id: int
) -> str | None:
    row = connection.execute(
        "SELECT token FROM push_endpoint"
        " WHERE person_id = ? AND active = 1 AND provider = 'FCM'"
        " ORDER BY COALESCE(last_seen_at, created_at) DESC, id DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    return None if row is None else str(row["token"])


def _sanitized_push_error(error: BaseException) -> str:
    """Return only a status code and provider code, never provider text."""
    status = getattr(error, "status_code", None)
    code = getattr(error, "provider_code", None)
    parts: list[str] = []
    if isinstance(status, int) and not isinstance(status, bool):
        parts.append(f"HTTP {status}")
    if isinstance(code, str) and code.strip():
        parts.append(code.strip())
    return " ".join(parts) if parts else "PROVIDER_ERROR"


def _finish(
    connection: sqlite3.Connection,
    *,
    notification_id: int,
    attempt_number: int,
    outcome: str,
    state: str,
    error: str | None,
    now: str,
) -> None:
    with transaction(connection):
        _insert_attempt(
            connection,
            notification_id=notification_id,
            attempt_number=attempt_number,
            outcome=outcome,
            error=error,
            recorded_at=now,
        )
        _set_delivery_state(connection, notification_id, state)


def deliver_pending(
    connection: sqlite3.Connection,
    *,
    push: PushSender | None,
    limit: int | None = None,
    now: str | None = None,
    settings: NotificationSettings | None = None,
) -> DeliveryBatch:
    """Deliver eligible notifications once, outside every transaction.

    Claiming is atomic; the provider call is made with no transaction open; the
    attempt row and state update are committed together afterwards. A failure
    only writes attempts and state, so the change that caused the notification
    is never rolled back (FR-NOT-04). A retry never inserts a second
    notification row (FR-NOT-05).

    ``limit`` stays an explicit override; when omitted it comes from the
    validated :class:`NotificationSettings.batch_limit` (O11-3), so the
    configured batch size is actually used.
    """
    if push is None:
        return DeliveryBatch(0, 0, 0, 0)
    effective = NotificationSettings() if settings is None else settings
    batch_limit = effective.batch_limit if limit is None else limit
    current = _utc_now() if now is None else now
    claimed = _claim(connection, limit=batch_limit, now=current, settings=effective)

    sent = 0
    retrying = 0
    exhausted = 0
    for notification_id, attempt_number in claimed:
        row = _notification_row(connection, notification_id)
        recipient_id = int(row["recipient_person_id"])
        person = connection.execute(
            "SELECT active FROM person WHERE id = ?", (recipient_id,)
        ).fetchone()
        if person is None or int(person["active"]) != 1:
            _finish(
                connection,
                notification_id=notification_id,
                attempt_number=attempt_number,
                outcome="NO_ENDPOINT",
                state="EXHAUSTED",
                error="RECIPIENT_INACTIVE",
                now=current,
            )
            exhausted += 1
            continue

        token = _active_endpoint_token(connection, recipient_id)
        if token is None:
            _finish(
                connection,
                notification_id=notification_id,
                attempt_number=attempt_number,
                outcome="NO_ENDPOINT",
                state="EXHAUSTED",
                error="NO_ACTIVE_ENDPOINT",
                now=current,
            )
            exhausted += 1
            continue

        try:
            view = render_notification(connection, notification_id)
        except NotificationRenderError:
            _finish(
                connection,
                notification_id=notification_id,
                attempt_number=attempt_number,
                outcome="FAILED",
                state="EXHAUSTED",
                error="RENDER_ERROR",
                now=current,
            )
            exhausted += 1
            continue

        try:
            push.send(
                token=token,
                title=view.title,
                body=view.body,
                deep_link=view.deep_link,
                collapse_key=str(notification_id),
            )
        except Exception as error:
            permanent = bool(getattr(error, "permanent", False))
            terminal = permanent or attempt_number >= effective.max_attempts
            _finish(
                connection,
                notification_id=notification_id,
                attempt_number=attempt_number,
                outcome="FAILED",
                state="EXHAUSTED" if terminal else "FAILED",
                error=_sanitized_push_error(error),
                now=current,
            )
            if terminal:
                exhausted += 1
            else:
                retrying += 1
        else:
            _finish(
                connection,
                notification_id=notification_id,
                attempt_number=attempt_number,
                outcome="SENT",
                state="SENT",
                error=None,
                now=current,
            )
            sent += 1

    return DeliveryBatch(
        attempted=len(claimed),
        sent=sent,
        retrying=retrying,
        exhausted=exhausted,
    )


def run_delivery_tick(
    connection: sqlite3.Connection,
    *,
    push: PushSender | None,
    otp_sender: OtpSender | None = None,
    limit: int | None = None,
    now: str | None = None,
    settings: NotificationSettings | None = None,
) -> DeliveryBatch:
    """One restart-safe delivery tick: sweep, then deliver pending rows.

    The tick holds no in-memory queue; every call re-reads durable state, so a
    crash between ticks is recoverable. The Phase 9 worker calls it after the
    pipeline tick (or a separate process calls it), and the deployment rule is
    exactly one delivery process (the worker).

    ``otp_sender`` is accepted for the Phase 12 boundary. The MVP notification
    schema has no OTP row and no OTP event type, so this tick invents neither;
    Phase 12 calls :meth:`OtpSender.send` directly after resolving the roster
    person's Discord id.
    """
    effective = NotificationSettings() if settings is None else settings
    sweep_unfanned_announcements(connection)
    return deliver_pending(
        connection,
        push=push,
        limit=limit,
        now=now,
        settings=effective,
    )


# --- announcement fan-out (W3; FR-NOT-06, FR-REV-10) -----------------------


def _announcement_recipients(
    connection: sqlite3.Connection, announcement: sqlite3.Row
) -> tuple[int, ...]:
    audience_type = str(announcement["audience_type"])
    if audience_type == "VERTICAL":
        rows = connection.execute(
            "SELECT p.id FROM person AS p"
            " JOIN person_vertical AS pv ON pv.person_id = p.id"
            " WHERE pv.vertical_id = ? AND p.active = 1"
            " ORDER BY p.id",
            (int(announcement["audience_vertical_id"]),),
        ).fetchall()
    elif audience_type == "AH_VOLUNTEERS":
        ah_person_id = announcement["audience_ah_person_id"]
        if ah_person_id is None:
            raise NotificationRenderError(
                "AH_VOLUNTEERS announcement has no sending AH"
            )
        rows = connection.execute(
            "SELECT id FROM person WHERE active = 1 AND role = 'Volunteer'"
            " AND reports_to = ? ORDER BY id",
            (int(ah_person_id),),
        ).fetchall()
    else:
        raise NotificationRenderError(f"unknown audience type {audience_type!r}")
    return tuple(int(row["id"]) for row in rows)


def fan_out_announcement(
    connection: sqlite3.Connection,
    announcement_id: int,
    *,
    now: str | None = None,
) -> int:
    """Stage one announcement notification per recipient; return rows inserted.

    The audience is the 0018 structured audience (PB-6). A zero-recipient
    audience stages zero rows and reports zero; it never falls back to the
    whole vertical. The body is rendered later from the accepted description.
    """
    announcement = connection.execute(
        "SELECT id, proposal_id, audience_type, audience_vertical_id,"
        " audience_ah_person_id FROM announcement WHERE id = ?",
        (announcement_id,),
    ).fetchone()
    if announcement is None:
        raise AnnouncementNotFoundError(announcement_id)
    proposal = connection.execute(
        "SELECT description FROM proposal WHERE id = ?",
        (int(announcement["proposal_id"]),),
    ).fetchone()
    if proposal is None:
        raise NotificationRenderError(
            f"announcement {announcement_id} has no proposal"
        )
    if proposal["description"] is None:
        raise NotificationRenderError(
            f"announcement {announcement_id} has no accepted description"
        )

    recipients = _announcement_recipients(connection, announcement)
    created_at = _utc_now() if now is None else now
    with transaction(connection):
        staged = 0
        for person_id in recipients:
            if stage_notification(
                connection,
                event_type=EVENT_ANNOUNCEMENT,
                recipient_person_id=person_id,
                dedupe_key=announcement_key(announcement_id, person_id),
                created_at=created_at,
                announcement_id=announcement_id,
            ):
                staged += 1
        return staged


def sweep_unfanned_announcements(
    connection: sqlite3.Connection,
) -> tuple[int, ...]:
    """Fan out accepted announcements that have no ``ANNOUNCEMENT:`` row.

    This is the restart-safe recovery path for a crash between accept and
    fan-out; it does not edit the Phase 6 accept transaction.
    """
    rows = connection.execute(
        "SELECT a.id FROM announcement AS a"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM notification AS n"
        "   WHERE n.announcement_id = a.id AND n.event_type = ?"
        " ) ORDER BY a.id",
        (EVENT_ANNOUNCEMENT,),
    ).fetchall()
    swept: list[int] = []
    for row in rows:
        announcement_id = int(row["id"])
        if fan_out_announcement(connection, announcement_id) > 0:
            swept.append(announcement_id)
    return tuple(swept)
