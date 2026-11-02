"""Cross-vertical approval reads (Phase 8, W1).

The approval service is the backend authority for FR-XV-03/04 reads. It never
touches task visibility: ``backend.tasks.visible_tasks`` remains the only live
task filter (Phase 7 contract), and this module reads the request tables plus
the task columns the approver is allowed to see.

Read-form authorization (plan §4.2, O8-1/O8-2):

* an active Admin may read any request;
* anyone else needs an ``approval_recipient`` row for *this* request, a current
  ``AH``/``Head`` role, and, for a Head, current membership of the responsible
  vertical. The recipient snapshot records who was asked; current roster state
  decides authority (FR-AUT-05).

A refused direct-id read is recorded in ``authorization_denial`` (FR-AUT-06).
The list form returns an empty queue instead of another team's requests.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.authorization import (
    AuthorizationError,
    record_denial,
    require_active_actor,
)
from backend.db.connection import transaction
from backend.notification_events import (
    EVENT_CROSS_VERTICAL_DECISION,
    EVENT_OPEN_TASK_AVAILABLE,
    EVENT_TASK_ASSIGNED,
    cross_vertical_decision_key,
    stage_notification,
    task_assignee_key,
)

OPERATION_APPROVAL_READ = "approval.read"
OPERATION_APPROVAL_DECIDE = "approval.decide"
SUBJECT_TYPE_APPROVAL_REQUEST = "approval_request"

REASON_NOT_APPROVAL_RECIPIENT = "NOT_APPROVAL_RECIPIENT"
REASON_APPROVAL_ROLE_NOT_PERMITTED = "APPROVAL_ROLE_NOT_PERMITTED"
REASON_HEAD_NOT_IN_RESPONSIBLE_VERTICAL = "HEAD_NOT_IN_RESPONSIBLE_VERTICAL"
REASON_STATE_CONTRADICTION = "STATE_CONTRADICTION"
REASON_INVALID_DECISION = "INVALID_DECISION"
REASON_BLANK_DECISION_MESSAGE = "BLANK_DECISION_MESSAGE"

DECISION_APPROVED = "APPROVED"
DECISION_DENIED = "DENIED"
DECISIONS = (DECISION_APPROVED, DECISION_DENIED)


class ApprovalError(Exception):
    """Base class for approval-service failures."""


class ApprovalNotFoundError(ApprovalError):
    """The requested approval request does not exist."""


class ApprovalAuthorizationError(AuthorizationError, ApprovalError):
    """The caller may not read or decide this approval request."""


class InvalidDecisionError(ApprovalError):
    """The decision token or the optional message is invalid."""


@dataclass(frozen=True)
class ApprovalView:
    """The approver-safe projection of one request (plan §4.5).

    Deliberately excludes source message content, bucket data and raw metadata:
    NFR-SEC-03(c) allows the description and the requester's note only.
    """

    request_id: int
    task_id: int
    requester_person_id: int
    requester_name: str
    request_message: str | None
    responsible_vertical_id: int
    responsible_vertical_name: str
    source_vertical_id: int
    source_vertical_name: str
    task_description: str
    task_type: str
    assignment_mode: str
    people_needed: int | None
    people_needed_unlimited: bool
    created_at: str
    decision: str | None
    decision_message: str | None


@dataclass(frozen=True)
class DecisionResult:
    """The observed outcome of one decision attempt (FR-XV-04)."""

    request_id: int
    task_id: int
    decision: str
    message: str | None
    decided_by: int
    decided_at: str
    changed: bool
    notified: int


class _DecisionDenied(Exception):
    """Internal: an authorization refusal raised inside the write transaction."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_REQUEST_SELECT = (
    "SELECT r.id AS request_id, r.task_id, r.requested_by, r.request_message,"
    " r.created_at, t.description, t.task_type, t.assignment_mode,"
    " t.people_needed, t.people_needed_unlimited, t.source_vertical_id,"
    " t.responsible_vertical_id, t.approval_state, requester.name AS requester_name,"
    " responsible.name AS responsible_vertical_name,"
    " source.name AS source_vertical_name,"
    " d.decision, d.decision_message"
    " FROM approval_request AS r"
    " JOIN task AS t ON t.id = r.task_id"
    " JOIN person AS requester ON requester.id = r.requested_by"
    " JOIN vertical AS responsible ON responsible.id = t.responsible_vertical_id"
    " JOIN vertical AS source ON source.id = t.source_vertical_id"
    " LEFT JOIN approval_decision AS d ON d.request_id = r.id"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_actor(
    connection: sqlite3.Connection,
    actor_person_id: int,
    *,
    operation: str,
    subject_id: int | None,
) -> sqlite3.Row:
    """Authenticate the caller, adapting the shared error to this service."""
    try:
        return require_active_actor(
            connection,
            actor_person_id,
            operation=operation,
            subject_type=SUBJECT_TYPE_APPROVAL_REQUEST,
            subject_id=subject_id,
        )
    except AuthorizationError as exc:
        raise ApprovalAuthorizationError(exc.reason) from exc


def _request_row(
    connection: sqlite3.Connection, request_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        f"{_REQUEST_SELECT} WHERE r.id = ?", (request_id,)
    ).fetchone()


def _is_recipient(
    connection: sqlite3.Connection, request_id: int, person_id: int
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM approval_recipient WHERE request_id = ? AND person_id = ?",
        (request_id, person_id),
    ).fetchone()
    return row is not None


def _is_member(
    connection: sqlite3.Connection, person_id: int, vertical_id: int
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM person_vertical WHERE person_id = ? AND vertical_id = ?",
        (person_id, vertical_id),
    ).fetchone()
    return row is not None


def _role(actor: sqlite3.Row) -> str:
    return str(actor["role"]).strip().casefold()


def _denial_reason(
    connection: sqlite3.Connection, actor: sqlite3.Row, row: sqlite3.Row
) -> str | None:
    """Return ``None`` when allowed, else the stable denial reason token."""
    role = _role(actor)
    if role == "admin":
        return None
    if role not in ("ah", "head"):
        return REASON_APPROVAL_ROLE_NOT_PERMITTED
    request_id = int(row["request_id"])
    person_id = int(actor["id"])
    if not _is_recipient(connection, request_id, person_id):
        return REASON_NOT_APPROVAL_RECIPIENT
    if role == "head" and not _is_member(
        connection, person_id, int(row["responsible_vertical_id"])
    ):
        return REASON_HEAD_NOT_IN_RESPONSIBLE_VERTICAL
    return None


def _view(row: sqlite3.Row) -> ApprovalView:
    return ApprovalView(
        request_id=int(row["request_id"]),
        task_id=int(row["task_id"]),
        requester_person_id=int(row["requested_by"]),
        requester_name=str(row["requester_name"]),
        request_message=row["request_message"],
        responsible_vertical_id=int(row["responsible_vertical_id"]),
        responsible_vertical_name=str(row["responsible_vertical_name"]),
        source_vertical_id=int(row["source_vertical_id"]),
        source_vertical_name=str(row["source_vertical_name"]),
        task_description=str(row["description"]),
        task_type=str(row["task_type"]),
        assignment_mode=str(row["assignment_mode"]),
        people_needed=(
            None if row["people_needed"] is None else int(row["people_needed"])
        ),
        people_needed_unlimited=bool(int(row["people_needed_unlimited"])),
        created_at=str(row["created_at"]),
        decision=None if row["decision"] is None else str(row["decision"]),
        decision_message=row["decision_message"],
    )


def get_approval_request(
    connection: sqlite3.Connection,
    request_id: int,
    *,
    actor_person_id: int,
) -> ApprovalView:
    """Return one request view, or raise for an unknown/unauthorized caller."""
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_APPROVAL_READ,
        subject_id=request_id,
    )
    row = _request_row(connection, request_id)
    if row is None:
        raise ApprovalNotFoundError(f"approval request {request_id} not found")
    reason = _denial_reason(connection, actor, row)
    if reason is not None:
        record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=OPERATION_APPROVAL_READ,
            subject_type=SUBJECT_TYPE_APPROVAL_REQUEST,
            subject_id=request_id,
            reason=reason,
        )
        raise ApprovalAuthorizationError(reason)
    return _view(row)


def list_pending_approvals(
    connection: sqlite3.Connection,
    *,
    actor_person_id: int,
) -> tuple[ApprovalView, ...]:
    """List undecided ``PENDING`` requests visible to the caller (FR-UI-03)."""
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_APPROVAL_READ,
        subject_id=None,
    )
    rows = connection.execute(
        f"{_REQUEST_SELECT}"
        " WHERE t.approval_state = 'PENDING' AND d.id IS NULL"
        " ORDER BY r.created_at, r.id"
    ).fetchall()
    if _role(actor) == "admin":
        return tuple(_view(row) for row in rows)
    return tuple(
        _view(row)
        for row in rows
        if _denial_reason(connection, actor, row) is None
    )


def _decision_row(
    connection: sqlite3.Connection, request_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, request_id, decided_by, decision, decision_message,"
        " decided_at FROM approval_decision WHERE request_id = ?",
        (request_id,),
    ).fetchone()


def _normalized_message(message: str | None) -> str | None:
    """Optional but never blank when supplied (0007 CHECK)."""
    if message is None:
        return None
    stripped = message.strip()
    if not stripped:
        raise InvalidDecisionError(REASON_BLANK_DECISION_MESSAGE)
    return stripped


def _actor_vertical_id(
    connection: sqlite3.Connection,
    actor_person_id: int,
    responsible_vertical_id: int,
) -> int | None:
    """FR-XV-06: the actor's vertical at action time, NULL without membership."""
    if _is_member(connection, actor_person_id, responsible_vertical_id):
        return responsible_vertical_id
    return None


def _awaiting_assignee_ids(
    connection: sqlite3.Connection, task_id: int
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT a.person_id FROM task_assignee AS a"
        " JOIN person AS p ON p.id = a.person_id"
        " WHERE a.task_id = ? AND a.agreement = 'AWAITING'"
        " AND a.cancelled = 0 AND p.active = 1"
        " ORDER BY a.id",
        (task_id,),
    ).fetchall()
    return tuple(int(row["person_id"]) for row in rows)


def _active_volunteer_ids(
    connection: sqlite3.Connection, vertical_id: int
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT DISTINCT p.id AS person_id FROM person AS p"
        " JOIN person_vertical AS pv ON pv.person_id = p.id"
        " WHERE pv.vertical_id = ? AND p.active = 1"
        " AND lower(trim(p.role)) = 'volunteer'"
        " ORDER BY p.id",
        (vertical_id,),
    ).fetchall()
    return tuple(int(row["person_id"]) for row in rows)


def _stage_decision_notifications(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    request_id: int,
    task_id: int,
    decision: str,
    now: str,
) -> int:
    """Stage the decision row and, on approval, the live-task rows (plan §4.4)."""
    requester_id = int(row["requested_by"])
    staged = int(
        stage_notification(
            connection,
            event_type=EVENT_CROSS_VERTICAL_DECISION,
            recipient_person_id=requester_id,
            dedupe_key=cross_vertical_decision_key(request_id, requester_id),
            approval_request_id=request_id,
            created_at=now,
        )
    )
    if decision != DECISION_APPROVED:
        return staged

    if str(row["assignment_mode"]) == "EXPLICIT":
        for person_id in _awaiting_assignee_ids(connection, task_id):
            staged += int(
                stage_notification(
                    connection,
                    event_type=EVENT_TASK_ASSIGNED,
                    recipient_person_id=person_id,
                    dedupe_key=task_assignee_key(
                        EVENT_TASK_ASSIGNED, task_id, person_id
                    ),
                    task_id=task_id,
                    created_at=now,
                )
            )
    else:
        for person_id in _active_volunteer_ids(
            connection, int(row["responsible_vertical_id"])
        ):
            staged += int(
                stage_notification(
                    connection,
                    event_type=EVENT_OPEN_TASK_AVAILABLE,
                    recipient_person_id=person_id,
                    dedupe_key=task_assignee_key(
                        EVENT_OPEN_TASK_AVAILABLE, task_id, person_id
                    ),
                    task_id=task_id,
                    created_at=now,
                )
            )
    return staged


def decide_request(
    connection: sqlite3.Connection,
    request_id: int,
    *,
    actor_person_id: int,
    decision: str,
    message: str | None = None,
) -> DecisionResult:
    """Resolve one request; the first decision wins (FR-XV-04, AC-15).

    One ``BEGIN IMMEDIATE`` transaction writes the decision, flips
    ``task.approval_state`` (never ``task.version``), appends the history row
    and stages notifications. A later attempt returns the stored decision with
    ``changed=False`` and writes nothing (O8-3). Authorization uses the current
    roster and is checked before the existing-decision path. A refusal rolls
    back first and is then recorded in ``authorization_denial`` (FR-AUT-06).
    """
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_APPROVAL_DECIDE,
        subject_id=request_id,
    )
    try:
        with transaction(connection):
            row = _request_row(connection, request_id)
            if row is None:
                raise ApprovalNotFoundError(
                    f"approval request {request_id} not found"
                )
            reason = _denial_reason(connection, actor, row)
            if reason is not None:
                raise _DecisionDenied(reason)
            if decision not in DECISIONS:
                raise InvalidDecisionError(REASON_INVALID_DECISION)
            decision_message = _normalized_message(message)
            task_id = int(row["task_id"])

            existing = _decision_row(connection, request_id)
            if existing is not None:
                return DecisionResult(
                    request_id=request_id,
                    task_id=task_id,
                    decision=str(existing["decision"]),
                    message=existing["decision_message"],
                    decided_by=int(existing["decided_by"]),
                    decided_at=str(existing["decided_at"]),
                    changed=False,
                    notified=0,
                )

            if str(row["approval_state"]) != "PENDING":
                raise ApprovalError(REASON_STATE_CONTRADICTION)

            now = _utc_now()
            connection.execute(
                "INSERT INTO approval_decision (request_id, decided_by, decision,"
                " decision_message, decided_at) VALUES (?, ?, ?, ?, ?)",
                (request_id, actor_person_id, decision, decision_message, now),
            )
            connection.execute(
                "UPDATE task SET approval_state = ? WHERE id = ?",
                (decision, task_id),
            )
            connection.execute(
                "INSERT INTO task_history (task_id, field_name, old_value,"
                " new_value, changed_at, actor_person_id, actor_role,"
                " actor_vertical_id, reason)"
                " VALUES (?, 'approval_state', 'PENDING', ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    decision,
                    now,
                    actor_person_id,
                    str(actor["role"]),
                    _actor_vertical_id(
                        connection,
                        actor_person_id,
                        int(row["responsible_vertical_id"]),
                    ),
                    decision_message or "CROSS_VERTICAL_DECISION",
                ),
            )
            notified = _stage_decision_notifications(
                connection,
                row=row,
                request_id=request_id,
                task_id=task_id,
                decision=decision,
                now=now,
            )
            return DecisionResult(
                request_id=request_id,
                task_id=task_id,
                decision=decision,
                message=decision_message,
                decided_by=actor_person_id,
                decided_at=now,
                changed=True,
                notified=notified,
            )
    except _DecisionDenied as exc:
        record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=OPERATION_APPROVAL_DECIDE,
            subject_type=SUBJECT_TYPE_APPROVAL_REQUEST,
            subject_id=request_id,
            reason=exc.reason,
        )
        raise ApprovalAuthorizationError(exc.reason) from None
