"""Task lifecycle service (Phase 7 W5/W6).

One visibility filter and one row loader serve every read and write (plan
§4.5/§4.6), so a hidden ``PENDING``/``DENIED`` task or a cancelled task behaves
as not found no matter which entry point is used (FR-TSK-11, NFR-SEC-03).

Boundary rules:

* Authority is derived **inside** the read functions from the current
  ``person``/``person_vertical`` rows; callers pass only ``viewer_person_id``
  (FR-AUT-01/02/05), so an HTTP layer cannot spoof a role.
* Scope follows the FR-AUT-04 matrix: Admin all; Head own verticals plus tasks
  they created; AH tasks they created; Volunteer own active assignments plus
  live ``OPEN`` tasks in their vertical (FR-TSK-04, FR-UI-02).
* ``include_hidden=True`` is the only way to see ``PENDING``/``DENIED`` rows and
  requires a current Admin; every other caller is denied and recorded
  (NFR-SEC-03(c), FR-AUT-06).
* Authorizations never read message content (FR-AUT-08); ``actor_person_id`` is
  the only identity input.
* Reverse references: ``backend.review`` materializes tasks but never imports
  this module; this module reads the rows it wrote.

W6 adds the lifecycle writes below the read model.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from backend.authorization import (
    AuthorizationError,
    record_denial,
    require_active_actor,
)
from backend.db.connection import transaction
from backend.notification_events import (
    EVENT_ASSIGNEE_ACCEPTED,
    EVENT_ASSIGNEE_DENIED,
    EVENT_TASK_CANCELLED,
    EVENT_TASK_EDITED,
    EVENT_TASK_REASSIGNED,
    EVENT_WORK_COMPLETED,
    stage_notification,
    task_assignee_key,
)

REASON_NOT_ADMIN = "NOT_ADMIN"
REASON_NOT_TASK_VIEWER = "NOT_TASK_VIEWER"
REASON_NOT_TASK_OWNER = "NOT_TASK_OWNER"
REASON_NOT_ASSIGNEE = "NOT_ASSIGNEE"
REASON_NOT_VOLUNTEER = "NOT_VOLUNTEER"
REASON_NOT_IN_VERTICAL = "NOT_IN_VERTICAL"

OPERATION_TASK_VIEW = "TASK_VIEW"
OPERATION_TASK_PROGRESS = "TASK_PROGRESS"
OPERATION_TASK_CLAIM = "TASK_CLAIM"
OPERATION_TASK_AGREEMENT = "TASK_AGREEMENT"
OPERATION_TASK_STATUS = "TASK_STATUS"
OPERATION_TASK_EDIT = "TASK_EDIT"
OPERATION_TASK_REASSIGN = "TASK_REASSIGN"
OPERATION_TASK_CANCEL = "TASK_CANCEL"

SUBJECT_TYPE_TASK = "TASK"

LIVE_APPROVAL_STATES = ("NOT_REQUIRED", "APPROVED")
AGREEMENTS = ("AWAITING", "ACCEPTED", "DENIED")
WORK_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED")
_CANCELLABLE_WORK_STATUSES = ("NOT_STARTED", "IN_PROGRESS")

_MISSING: object = object()


class TaskError(Exception):
    """Base class for task-service failures."""


class TaskNotFoundError(TaskError):
    """The task id does not exist or is hidden from the lifecycle."""


class TaskAuthorizationError(AuthorizationError, TaskError):
    """The caller may not perform this operation on this task."""


class TaskStateError(TaskError):
    """The requested transition or value is not allowed; ``code`` is stable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TaskCapacityError(TaskError):
    """An OPEN task has no remaining place (FR-TSK-04, AC-08)."""


class InvalidAgreementError(TaskError):
    """The agreement value is not one of the SRS tokens (FR-TSK-03)."""

    def __init__(self, agreement: str) -> None:
        self.agreement = agreement
        super().__init__(f"invalid agreement: {agreement}")


class StaleTaskVersionError(TaskError):
    """``expected_version`` does not match the stored version (FR-TSK-12)."""

    def __init__(
        self,
        expected: int,
        current: int,
        snapshot: TaskSummary,
    ) -> None:
        self.expected = expected
        self.current = current
        self.snapshot = snapshot
        super().__init__(
            f"stale task version: expected {expected}, current {current}"
        )


def _require_count_row(row: sqlite3.Row | None) -> sqlite3.Row:
    """Return a COUNT row or raise the task domain error (W9 H3).

    This replaces a runtime ``assert`` so the check survives ``python -O``. The
    message carries no user content.
    """
    if row is None:
        raise TaskStateError("capacity count query returned no row")
    return row


@dataclass(frozen=True)
class TaskEdit:
    """An edit request; ``_MISSING`` means "keep the current value".

    ``attributes`` replaces the whole attribute set (``None`` clears it) and
    keys must be unique, mirroring ``ProposalEdit`` (plan §4.6).
    """

    description: object = _MISSING
    people_needed: object = _MISSING
    attributes: object = _MISSING


@dataclass(frozen=True)
class TaskSummary:
    """One row of the task read model (plan §4.5)."""

    id: int
    task_type: str
    source_vertical_id: int
    responsible_vertical_id: int
    creator_id: int
    description: str
    assignment_mode: str
    people_needed: int | None
    people_needed_unlimited: bool
    cancellation: str
    version: int
    accepted_count: int
    completed_count: int


@dataclass(frozen=True)
class TaskAssigneeProgress:
    """One assignee row in the progress roll-up (FR-TSK-07)."""

    person_id: int
    name: str
    agreement: str
    work_status: str
    cancelled: bool


@dataclass(frozen=True)
class TaskProgress:
    """Per-assignee progress plus the accepted/completed roll-up (AC-16)."""

    task_id: int
    people_needed: int | None
    accepted_count: int
    completed_count: int
    assignees: tuple[TaskAssigneeProgress, ...]


def _role(actor: sqlite3.Row) -> str:
    return str(actor["role"]).strip().casefold()


def _require_actor(
    connection: sqlite3.Connection,
    actor_person_id: int,
    *,
    operation: str,
    subject_id: int | None,
) -> sqlite3.Row:
    """Authenticate the caller, adapting the shared error to the task service."""
    try:
        return require_active_actor(
            connection,
            actor_person_id,
            operation=operation,
            subject_type=SUBJECT_TYPE_TASK,
            subject_id=subject_id,
        )
    except AuthorizationError as exc:
        raise TaskAuthorizationError(exc.reason) from exc


def _deny_and_raise(
    connection: sqlite3.Connection,
    actor_person_id: int | None,
    *,
    operation: str,
    subject_id: int | None,
    reason: str,
) -> NoReturn:
    """Record the denied attempt in its own transaction, then raise (FR-AUT-06)."""
    record_denial(
        connection,
        actor_person_id=actor_person_id,
        operation=operation,
        subject_type=SUBJECT_TYPE_TASK,
        subject_id=subject_id,
        reason=reason,
    )
    raise TaskAuthorizationError(reason)


def _visibility_clauses(
    connection: sqlite3.Connection,
    actor: sqlite3.Row,
    *,
    viewer_person_id: int,
    include_cancelled: bool,
    include_hidden: bool,
) -> tuple[list[str], list[object]]:
    """Build the single visibility predicate (plan §4.5) for one viewer."""
    clauses: list[str] = []
    parameters: list[object] = []
    if not include_hidden:
        clauses.append("t.approval_state IN ('NOT_REQUIRED', 'APPROVED')")
    if not include_cancelled:
        clauses.append("t.cancellation = 'ACTIVE'")

    role = _role(actor)
    if role == "admin":
        return clauses or ["1 = 1"], parameters
    if role == "head":
        clauses.append(
            "(t.source_vertical_id IN (SELECT vertical_id FROM person_vertical"
            " WHERE person_id = ?)"
            " OR t.responsible_vertical_id IN (SELECT vertical_id FROM"
            " person_vertical WHERE person_id = ?)"
            " OR t.creator_id = ?)"
        )
        parameters.extend([viewer_person_id, viewer_person_id, viewer_person_id])
        return clauses, parameters
    if role == "ah":
        clauses.append("t.creator_id = ?")
        parameters.append(viewer_person_id)
        return clauses, parameters
    if role == "volunteer":
        clauses.append(
            "(EXISTS (SELECT 1 FROM task_assignee AS a WHERE a.task_id = t.id"
            " AND a.person_id = ? AND a.agreement != 'DENIED'"
            " AND a.cancelled = 0)"
            " OR (t.assignment_mode = 'OPEN' AND t.responsible_vertical_id IN"
            " (SELECT vertical_id FROM person_vertical WHERE person_id = ?)))"
        )
        parameters.extend([viewer_person_id, viewer_person_id])
        return clauses, parameters
    # 0016's CHECK makes an unknown role unreachable; deny rather than widen.
    _deny_and_raise(
        connection,
        viewer_person_id,
        operation=OPERATION_TASK_VIEW,
        subject_id=None,
        reason=REASON_NOT_TASK_VIEWER,
    )


_SUMMARY_SELECT = (
    "SELECT t.id, t.task_type, t.source_vertical_id, t.responsible_vertical_id,"
    " t.creator_id, t.description, t.assignment_mode, t.people_needed,"
    " t.people_needed_unlimited, t.cancellation, t.version,"
    " (SELECT COUNT(*) FROM task_assignee AS a WHERE a.task_id = t.id"
    " AND a.agreement = 'ACCEPTED' AND a.cancelled = 0) AS accepted_count,"
    " (SELECT COUNT(*) FROM task_assignee AS a WHERE a.task_id = t.id"
    " AND a.work_status = 'COMPLETED' AND a.cancelled = 0) AS completed_count"
    " FROM task AS t"
)


def _summary(row: sqlite3.Row) -> TaskSummary:
    return TaskSummary(
        id=int(row["id"]),
        task_type=str(row["task_type"]),
        source_vertical_id=int(row["source_vertical_id"]),
        responsible_vertical_id=int(row["responsible_vertical_id"]),
        creator_id=int(row["creator_id"]),
        description=str(row["description"]),
        assignment_mode=str(row["assignment_mode"]),
        people_needed=(
            None if row["people_needed"] is None else int(row["people_needed"])
        ),
        people_needed_unlimited=bool(int(row["people_needed_unlimited"])),
        cancellation=str(row["cancellation"]),
        version=int(row["version"]),
        accepted_count=int(row["accepted_count"]),
        completed_count=int(row["completed_count"]),
    )


def visible_tasks(
    connection: sqlite3.Connection,
    *,
    viewer_person_id: int,
    include_cancelled: bool = False,
    include_hidden: bool = False,
) -> tuple[TaskSummary, ...]:
    """List the live tasks the viewer may see (plan §4.5, FR-UI-02).

    ``include_hidden`` requires a current Admin and is the only path that lists
    ``PENDING``/``DENIED`` tasks; a non-Admin request is recorded and denied.
    """
    actor = _require_actor(
        connection,
        viewer_person_id,
        operation=OPERATION_TASK_VIEW,
        subject_id=None,
    )
    if include_hidden and _role(actor) != "admin":
        _deny_and_raise(
            connection,
            viewer_person_id,
            operation=OPERATION_TASK_VIEW,
            subject_id=None,
            reason=REASON_NOT_ADMIN,
        )

    clauses, parameters = _visibility_clauses(
        connection,
        actor,
        viewer_person_id=viewer_person_id,
        include_cancelled=include_cancelled,
        include_hidden=include_hidden,
    )
    rows = connection.execute(
        f"{_SUMMARY_SELECT} WHERE {' AND '.join(clauses)} ORDER BY t.id",
        parameters,
    ).fetchall()
    return tuple(_summary(row) for row in rows)


def _live_task_row(
    connection: sqlite3.Connection, task_id: int
) -> sqlite3.Row | None:
    """The single row loader for every lifecycle read and write (plan §4.5).

    A ``PENDING``/``DENIED`` or cancelled task is returned as ``None``, so a
    hidden id passed directly behaves as not found.
    """
    return connection.execute(
        "SELECT * FROM task WHERE id = ?"
        " AND approval_state IN ('NOT_REQUIRED', 'APPROVED')"
        " AND cancellation = 'ACTIVE'",
        (task_id,),
    ).fetchone()


def _is_vertical_member(
    connection: sqlite3.Connection,
    person_id: int,
    task: sqlite3.Row,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM person_vertical WHERE person_id = ?"
        " AND vertical_id IN (?, ?)",
        (
            person_id,
            int(task["source_vertical_id"]),
            int(task["responsible_vertical_id"]),
        ),
    ).fetchone()
    return row is not None


def _may_read_progress(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    actor: sqlite3.Row,
    viewer_person_id: int,
) -> bool:
    """Matrix row 10: Admin all; Head own vertical; AH creator; Volunteer own."""
    role = _role(actor)
    if role == "admin":
        return True
    if role == "head":
        return _is_vertical_member(connection, viewer_person_id, task)
    if role == "ah":
        return int(task["creator_id"]) == viewer_person_id
    if role == "volunteer":
        row = connection.execute(
            "SELECT 1 FROM task_assignee WHERE task_id = ? AND person_id = ?"
            " AND agreement != 'DENIED' AND cancelled = 0",
            (int(task["id"]), viewer_person_id),
        ).fetchone()
        return row is not None
    return False


def task_progress(
    connection: sqlite3.Connection,
    task_id: int,
    *,
    viewer_person_id: int,
) -> TaskProgress:
    """Read the per-assignee progress and roll-up for one live task (FR-TSK-07)."""
    actor = _require_actor(
        connection,
        viewer_person_id,
        operation=OPERATION_TASK_PROGRESS,
        subject_id=task_id,
    )
    task = _live_task_row(connection, task_id)
    if task is None:
        raise TaskNotFoundError(f"task {task_id} not found")
    if not _may_read_progress(connection, task, actor, viewer_person_id):
        _deny_and_raise(
            connection,
            viewer_person_id,
            operation=OPERATION_TASK_PROGRESS,
            subject_id=task_id,
            reason=REASON_NOT_TASK_VIEWER,
        )

    rows = connection.execute(
        "SELECT a.person_id, p.name, a.agreement, a.work_status, a.cancelled"
        " FROM task_assignee AS a"
        " JOIN person AS p ON p.id = a.person_id"
        " WHERE a.task_id = ? ORDER BY a.id",
        (task_id,),
    ).fetchall()
    assignees = tuple(
        TaskAssigneeProgress(
            person_id=int(row["person_id"]),
            name=str(row["name"]),
            agreement=str(row["agreement"]),
            work_status=str(row["work_status"]),
            cancelled=bool(int(row["cancelled"])),
        )
        for row in rows
    )
    accepted_count = sum(
        1
        for assignee in assignees
        if assignee.agreement == "ACCEPTED" and not assignee.cancelled
    )
    completed_count = sum(
        1
        for assignee in assignees
        if assignee.work_status == "COMPLETED" and not assignee.cancelled
    )
    return TaskProgress(
        task_id=task_id,
        people_needed=(
            None if task["people_needed"] is None else int(task["people_needed"])
        ),
        accepted_count=accepted_count,
        completed_count=completed_count,
        assignees=assignees,
    )


# ---------------------------------------------------------------------------
# W6 - lifecycle writes (plan §4.6). Every write authenticates first, loads the
# one live row, checks authority, then writes in a single BEGIN IMMEDIATE
# transaction; denials are recorded after that transaction has rolled back.
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _actor_vertical_id(
    connection: sqlite3.Connection,
    actor_person_id: int,
    task: sqlite3.Row | None,
) -> int | None:
    """The vertical the action was taken in, for FR-TSK-11's history copy."""
    rows = connection.execute(
        "SELECT vertical_id FROM person_vertical WHERE person_id = ?"
        " ORDER BY vertical_id",
        (actor_person_id,),
    ).fetchall()
    vertical_ids = [int(row["vertical_id"]) for row in rows]
    if task is not None:
        for candidate in (
            int(task["responsible_vertical_id"]),
            int(task["source_vertical_id"]),
        ):
            if candidate in vertical_ids:
                return candidate
    return vertical_ids[0] if vertical_ids else None


def _record_history(
    connection: sqlite3.Connection,
    *,
    task_id: int,
    field_name: str,
    old_value: object,
    new_value: object,
    actor: sqlite3.Row,
    task: sqlite3.Row | None,
    reason: str,
    changed_at: str,
) -> None:
    """Append one FR-TSK-11 history row; there is no update or delete path."""
    connection.execute(
        "INSERT INTO task_history (task_id, field_name, old_value, new_value,"
        " changed_at, actor_person_id, actor_model_id, actor_model_version,"
        " actor_role, actor_vertical_id, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
        (
            task_id,
            field_name,
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            changed_at,
            int(actor["id"]),
            str(actor["role"]),
            _actor_vertical_id(connection, int(actor["id"]), task),
            reason,
        ),
    )


def _notify(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    task_id: int,
    recipient_person_id: int,
    created_at: str,
) -> bool:
    return stage_notification(
        connection,
        event_type=event_type,
        recipient_person_id=recipient_person_id,
        dedupe_key=task_assignee_key(event_type, task_id, recipient_person_id),
        created_at=created_at,
        task_id=task_id,
    )


def _active_assignee_person_ids(
    connection: sqlite3.Connection, task_id: int
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT person_id FROM task_assignee WHERE task_id = ? AND cancelled = 0"
        " AND agreement != 'DENIED' ORDER BY id",
        (task_id,),
    ).fetchall()
    return tuple(int(row["person_id"]) for row in rows)


def _active_assignment(
    connection: sqlite3.Connection, task_id: int, person_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM task_assignee WHERE task_id = ? AND person_id = ?"
        " AND agreement != 'DENIED' AND cancelled = 0",
        (task_id, person_id),
    ).fetchone()


def _is_admin(actor: sqlite3.Row) -> bool:
    return _role(actor) == "admin"


def _is_member(
    connection: sqlite3.Connection, person_id: int, vertical_id: int
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM person_vertical WHERE person_id = ? AND vertical_id = ?",
        (person_id, vertical_id),
    ).fetchone()
    return row is not None


def _require_task_owner(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    actor: sqlite3.Row,
    actor_person_id: int,
) -> None:
    """Creator, Head of the responsible vertical, or Admin (FR-TSK-16)."""
    if _is_admin(actor):
        return
    if int(task["creator_id"]) == actor_person_id:
        return
    if _role(actor) == "head" and _is_member(
        connection, actor_person_id, int(task["responsible_vertical_id"])
    ):
        return
    raise TaskAuthorizationError(REASON_NOT_TASK_OWNER)


def _current_summary(connection: sqlite3.Connection, task_id: int) -> TaskSummary:
    row = connection.execute(
        f"{_SUMMARY_SELECT} WHERE t.id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"task {task_id} not found")
    return _summary(row)


def _record_stale_history(
    connection: sqlite3.Connection,
    task_id: int,
    actor: sqlite3.Row,
    error: StaleTaskVersionError,
) -> None:
    """Record a stale attempt in its own committed transaction (FR-TSK-12)."""
    task = _live_task_row(connection, task_id)
    with transaction(connection):
        _record_history(
            connection,
            task_id=task_id,
            field_name="version",
            old_value=error.expected,
            new_value=error.current,
            actor=actor,
            task=task,
            reason="STALE_EDIT",
            changed_at=_now(),
        )


def claim_open_task(
    connection: sqlite3.Connection, task_id: int, actor_person_id: int
) -> int:
    """Claim one place on an OPEN task (FR-TSK-04, AC-08); returns the row id."""
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_TASK_CLAIM,
        subject_id=task_id,
    )
    try:
        with transaction(connection):
            task = _live_task_row(connection, task_id)
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            if _role(actor) != "volunteer":
                raise TaskAuthorizationError(REASON_NOT_VOLUNTEER)
            if not _is_member(
                connection, actor_person_id, int(task["responsible_vertical_id"])
            ):
                raise TaskAuthorizationError(REASON_NOT_IN_VERTICAL)
            if str(task["assignment_mode"]) != "OPEN":
                raise TaskStateError("NOT_OPEN")
            if _active_assignment(connection, task_id, actor_person_id) is not None:
                raise TaskStateError("ALREADY_JOINED")
            if task["people_needed"] is not None:
                count_row = _require_count_row(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM task_assignee WHERE task_id = ?"
                        " AND agreement = 'ACCEPTED' AND cancelled = 0",
                        (task_id,),
                    ).fetchone()
                )
                if int(count_row["n"]) >= int(task["people_needed"]):
                    raise TaskCapacityError("task is full")

            now = _now()
            cursor = connection.execute(
                "INSERT INTO task_assignee (task_id, person_id, agreement,"
                " agreed_at, work_status, cancelled, added_at)"
                " VALUES (?, ?, 'ACCEPTED', ?, 'NOT_STARTED', 0, ?)",
                (task_id, actor_person_id, now, now),
            )
            _record_history(
                connection,
                task_id=task_id,
                field_name="agreement",
                old_value=None,
                new_value="ACCEPTED",
                actor=actor,
                task=task,
                reason="CLAIM",
                changed_at=now,
            )
            _notify(
                connection,
                event_type=EVENT_ASSIGNEE_ACCEPTED,
                task_id=task_id,
                recipient_person_id=int(task["creator_id"]),
                created_at=now,
            )
            return int(cursor.lastrowid or 0)
    except TaskAuthorizationError as error:
        _deny_and_raise(
            connection,
            actor_person_id,
            operation=OPERATION_TASK_CLAIM,
            subject_id=task_id,
            reason=error.reason,
        )


def set_agreement(
    connection: sqlite3.Connection,
    task_id: int,
    actor_person_id: int,
    assignee_person_id: int,
    agreement: str,
) -> None:
    """Accept or deny one AWAITING assignment (FR-TSK-03/05; O7-3)."""
    if agreement not in ("ACCEPTED", "DENIED"):
        raise InvalidAgreementError(agreement)
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_TASK_AGREEMENT,
        subject_id=task_id,
    )
    on_behalf = actor_person_id != assignee_person_id
    try:
        with transaction(connection):
            task = _live_task_row(connection, task_id)
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            if on_behalf and not _is_admin(actor):
                raise TaskAuthorizationError(REASON_NOT_ASSIGNEE)

            row = _active_assignment(connection, task_id, assignee_person_id)
            if row is None:
                denied_row = connection.execute(
                    "SELECT 1 FROM task_assignee WHERE task_id = ? AND person_id = ?"
                    " AND agreement = 'DENIED'",
                    (task_id, assignee_person_id),
                ).fetchone()
                raise TaskStateError(
                    "ALREADY_DENIED" if denied_row is not None else "NOT_ASSIGNED"
                )
            if str(row["agreement"]) != "AWAITING":
                current = str(row["agreement"])
                if current == agreement:
                    raise TaskStateError("NO_CHANGE")
                raise TaskStateError("ALREADY_DECIDED")

            now = _now()
            connection.execute(
                "UPDATE task_assignee SET agreement = ?, agreed_at = ? WHERE id = ?",
                (agreement, now, int(row["id"])),
            )
            _record_history(
                connection,
                task_id=task_id,
                field_name="agreement",
                old_value="AWAITING",
                new_value=agreement,
                actor=actor,
                task=task,
                reason="ADMIN_ON_BEHALF" if on_behalf else "SELF_ACTION",
                changed_at=now,
            )
            _notify(
                connection,
                event_type=(
                    EVENT_ASSIGNEE_ACCEPTED
                    if agreement == "ACCEPTED"
                    else EVENT_ASSIGNEE_DENIED
                ),
                task_id=task_id,
                recipient_person_id=int(task["creator_id"]),
                created_at=now,
            )
    except TaskAuthorizationError as error:
        _deny_and_raise(
            connection,
            actor_person_id,
            operation=OPERATION_TASK_AGREEMENT,
            subject_id=task_id,
            reason=error.reason,
        )


def update_work_status(
    connection: sqlite3.Connection,
    task_id: int,
    actor_person_id: int,
    status: str,
    *,
    assignee_person_id: int | None = None,
) -> None:
    """Advance one accepted assignee's work status (FR-TSK-06/07; O7-5)."""
    target_person_id = (
        actor_person_id if assignee_person_id is None else assignee_person_id
    )
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_TASK_STATUS,
        subject_id=task_id,
    )
    override = target_person_id != actor_person_id
    try:
        with transaction(connection):
            task = _live_task_row(connection, task_id)
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            if override and not _is_admin(actor):
                raise TaskAuthorizationError(REASON_NOT_ASSIGNEE)

            row = connection.execute(
                "SELECT * FROM task_assignee WHERE task_id = ? AND person_id = ?"
                " AND agreement = 'ACCEPTED' AND cancelled = 0",
                (task_id, target_person_id),
            ).fetchone()
            if row is None:
                raise TaskStateError("NOT_ACCEPTED")
            current = str(row["work_status"])
            if status == current:
                raise TaskStateError("NO_CHANGE")
            if current == "COMPLETED":
                raise TaskStateError("COMPLETED_FINAL")
            if (current, status) not in (
                ("NOT_STARTED", "IN_PROGRESS"),
                ("IN_PROGRESS", "COMPLETED"),
            ):
                raise TaskStateError("INVALID_TRANSITION")

            now = _now()
            connection.execute(
                "UPDATE task_assignee SET work_status = ? WHERE id = ?",
                (status, int(row["id"])),
            )
            _record_history(
                connection,
                task_id=task_id,
                field_name="work_status",
                old_value=current,
                new_value=status,
                actor=actor,
                task=task,
                reason="ADMIN_OVERRIDE" if override else "STATUS_UPDATE",
                changed_at=now,
            )
            if status == "COMPLETED":
                _notify(
                    connection,
                    event_type=EVENT_WORK_COMPLETED,
                    task_id=task_id,
                    recipient_person_id=int(task["creator_id"]),
                    created_at=now,
                )
    except TaskAuthorizationError as error:
        _deny_and_raise(
            connection,
            actor_person_id,
            operation=OPERATION_TASK_STATUS,
            subject_id=task_id,
            reason=error.reason,
        )


def _clean_description(value: object) -> str:
    if not isinstance(value, str):
        raise TaskStateError("INVALID_DESCRIPTION")
    cleaned = value.strip()
    if not cleaned:
        raise TaskStateError("INVALID_DESCRIPTION")
    return cleaned


def _normalized_attributes(
    value: object,
) -> tuple[tuple[str, str | None], ...]:
    """Validate a replacement attribute set; keys must be unique (plan §4.6)."""
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise TaskStateError("INVALID_ATTRIBUTE")
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TaskStateError("INVALID_ATTRIBUTE")
        key, raw_value = item
        if not isinstance(key, str) or not key.strip():
            raise TaskStateError("INVALID_ATTRIBUTE")
        clean_key = key.strip()
        if clean_key in seen:
            raise TaskStateError("DUPLICATE_ATTRIBUTE")
        seen.add(clean_key)
        pairs.append((clean_key, None if raw_value is None else str(raw_value)))
    return tuple(pairs)


def _plan_edit(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    task_id: int,
    changes: TaskEdit,
) -> tuple[
    dict[str, object],
    list[tuple[str, object, object]],
    tuple[tuple[str, str | None], ...] | None,
]:
    """Validate an edit and return scalar updates, history rows and attributes."""
    updates: dict[str, object] = {}
    history: list[tuple[str, object, object]] = []
    attributes: tuple[tuple[str, str | None], ...] | None = None

    if changes.description is not _MISSING:
        description = _clean_description(changes.description)
        if description != str(task["description"]):
            updates["description"] = description
            history.append(("description", str(task["description"]), description))

    if changes.people_needed is not _MISSING:
        if str(task["assignment_mode"]) != "OPEN":
            raise TaskStateError("NOT_OPEN")
        if bool(int(task["people_needed_unlimited"])):
            raise TaskStateError("UNLIMITED_CAPACITY")
        value = changes.people_needed
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TaskStateError("INVALID_PEOPLE_NEEDED")
        count_row = _require_count_row(
            connection.execute(
                "SELECT COUNT(*) AS n FROM task_assignee WHERE task_id = ?"
                " AND agreement = 'ACCEPTED' AND cancelled = 0",
                (task_id,),
            ).fetchone()
        )
        if value < int(count_row["n"]):
            raise TaskStateError("CAPACITY_BELOW_ACCEPTED")
        current = task["people_needed"]
        if current is None or int(current) != value:
            updates["people_needed"] = value
            history.append(
                ("people_needed", None if current is None else int(current), value)
            )

    if changes.attributes is not _MISSING:
        attributes = _normalized_attributes(changes.attributes)
        rows = connection.execute(
            "SELECT attr_key, attr_value FROM task_attribute WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        current_attributes = {
            str(row["attr_key"]): (
                None if row["attr_value"] is None else str(row["attr_value"])
            )
            for row in rows
        }
        replacement = dict(attributes)
        if current_attributes != replacement:
            for key in sorted(set(current_attributes) | set(replacement)):
                old_value = current_attributes.get(key, _MISSING)
                new_value = replacement.get(key, _MISSING)
                if old_value is _MISSING and new_value is _MISSING:
                    continue
                if old_value != new_value:
                    history.append(
                        (
                            f"attribute:{key}",
                            None if old_value is _MISSING else old_value,
                            None if new_value is _MISSING else new_value,
                        )
                    )
        else:
            attributes = None

    return updates, history, attributes


def edit_task(
    connection: sqlite3.Connection,
    task_id: int,
    actor_person_id: int,
    expected_version: int,
    changes: TaskEdit,
) -> int:
    """Edit description/capacity/attributes and bump the version (FR-TSK-16)."""
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_TASK_EDIT,
        subject_id=task_id,
    )
    try:
        with transaction(connection):
            task = _live_task_row(connection, task_id)
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            _require_task_owner(connection, task, actor, actor_person_id)

            current_version = int(task["version"])
            if expected_version != current_version:
                raise StaleTaskVersionError(
                    expected_version,
                    current_version,
                    _current_summary(connection, task_id),
                )

            updates, history, attributes = _plan_edit(
                connection, task, task_id, changes
            )
            if not updates and not history and attributes is None:
                raise TaskStateError("NO_CHANGE")

            now = _now()
            if "description" in updates:
                connection.execute(
                    "UPDATE task SET description = ? WHERE id = ?",
                    (updates["description"], task_id),
                )
            if "people_needed" in updates:
                connection.execute(
                    "UPDATE task SET people_needed = ? WHERE id = ?",
                    (updates["people_needed"], task_id),
                )
            if attributes is not None:
                connection.execute(
                    "DELETE FROM task_attribute WHERE task_id = ?", (task_id,)
                )
                connection.executemany(
                    "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
                    " VALUES (?, ?, ?)",
                    [(task_id, key, value) for key, value in attributes],
                )
            connection.execute(
                "UPDATE task SET version = version + 1 WHERE id = ?", (task_id,)
            )
            for field_name, old_value, new_value in history:
                _record_history(
                    connection,
                    task_id=task_id,
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    actor=actor,
                    task=task,
                    reason="EDIT",
                    changed_at=now,
                )
            for person_id in _active_assignee_person_ids(connection, task_id):
                _notify(
                    connection,
                    event_type=EVENT_TASK_EDITED,
                    task_id=task_id,
                    recipient_person_id=person_id,
                    created_at=now,
                )
            return current_version + 1
    except TaskAuthorizationError as error:
        _deny_and_raise(
            connection,
            actor_person_id,
            operation=OPERATION_TASK_EDIT,
            subject_id=task_id,
            reason=error.reason,
        )
    except StaleTaskVersionError as error:
        _record_stale_history(connection, task_id, actor, error)
        raise


def reassign_task(
    connection: sqlite3.Connection,
    task_id: int,
    actor_person_id: int,
    expected_version: int,
    *,
    add: tuple[int, ...] = (),
    remove: tuple[int, ...] = (),
) -> int:
    """Add and/or remove assignees with history and notices (FR-TSK-10/16)."""
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_TASK_REASSIGN,
        subject_id=task_id,
    )
    add_ids = tuple(dict.fromkeys(int(person) for person in add))
    remove_ids = tuple(dict.fromkeys(int(person) for person in remove))
    try:
        with transaction(connection):
            task = _live_task_row(connection, task_id)
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            _require_task_owner(connection, task, actor, actor_person_id)

            current_version = int(task["version"])
            if expected_version != current_version:
                raise StaleTaskVersionError(
                    expected_version,
                    current_version,
                    _current_summary(connection, task_id),
                )

            removals: list[tuple[int, int]] = []
            for person_id in remove_ids:
                row = _active_assignment(connection, task_id, person_id)
                if row is None:
                    raise TaskStateError("NOT_ASSIGNED")
                removals.append((int(row["id"]), person_id))

            additions: list[int] = []
            for person_id in add_ids:
                person = connection.execute(
                    "SELECT id, active FROM person WHERE id = ?", (person_id,)
                ).fetchone()
                if person is None:
                    raise TaskStateError("ASSIGNEE_NOT_FOUND")
                if int(person["active"]) != 1:
                    raise TaskStateError("ASSIGNEE_INACTIVE")
                if (
                    person_id not in remove_ids
                    and _active_assignment(connection, task_id, person_id)
                    is not None
                ):
                    raise TaskStateError("ALREADY_ASSIGNED")
                additions.append(person_id)

            if not removals and not additions:
                raise TaskStateError("NO_CHANGE")

            now = _now()
            for row_id, person_id in removals:
                connection.execute(
                    "UPDATE task_assignee SET cancelled = 1 WHERE id = ?",
                    (row_id,),
                )
                _record_history(
                    connection,
                    task_id=task_id,
                    field_name="assignee",
                    old_value=person_id,
                    new_value="REMOVED",
                    actor=actor,
                    task=task,
                    reason="REASSIGN",
                    changed_at=now,
                )
            for person_id in additions:
                connection.execute(
                    "INSERT INTO task_assignee (task_id, person_id, agreement,"
                    " agreed_at, work_status, cancelled, added_at)"
                    " VALUES (?, ?, 'AWAITING', NULL, 'NOT_STARTED', 0, ?)",
                    (task_id, person_id, now),
                )
                _record_history(
                    connection,
                    task_id=task_id,
                    field_name="assignee",
                    old_value=None,
                    new_value=person_id,
                    actor=actor,
                    task=task,
                    reason="REASSIGN",
                    changed_at=now,
                )
            connection.execute(
                "UPDATE task SET version = version + 1 WHERE id = ?", (task_id,)
            )
            for person_id in dict.fromkeys((*remove_ids, *add_ids)):
                _notify(
                    connection,
                    event_type=EVENT_TASK_REASSIGNED,
                    task_id=task_id,
                    recipient_person_id=person_id,
                    created_at=now,
                )
            return current_version + 1
    except TaskAuthorizationError as error:
        _deny_and_raise(
            connection,
            actor_person_id,
            operation=OPERATION_TASK_REASSIGN,
            subject_id=task_id,
            reason=error.reason,
        )
    except StaleTaskVersionError as error:
        _record_stale_history(connection, task_id, actor, error)
        raise


def cancel_task(
    connection: sqlite3.Connection,
    task_id: int,
    actor_person_id: int,
    reason: str,
) -> None:
    """Cancel a live task and its unfinished assignments (FR-TSK-08; O7-4)."""
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        raise TaskStateError("REASON_REQUIRED")
    actor = _require_actor(
        connection,
        actor_person_id,
        operation=OPERATION_TASK_CANCEL,
        subject_id=task_id,
    )
    try:
        with transaction(connection):
            task = _live_task_row(connection, task_id)
            if task is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            _require_task_owner(connection, task, actor, actor_person_id)

            now = _now()
            rows = connection.execute(
                "SELECT id, person_id FROM task_assignee WHERE task_id = ?"
                " AND cancelled = 0 AND work_status IN ('NOT_STARTED',"
                " 'IN_PROGRESS') ORDER BY id",
                (task_id,),
            ).fetchall()
            connection.execute(
                "UPDATE task SET cancellation = 'CANCELLED',"
                " version = version + 1 WHERE id = ?",
                (task_id,),
            )
            for row in rows:
                connection.execute(
                    "UPDATE task_assignee SET cancelled = 1 WHERE id = ?",
                    (int(row["id"]),),
                )
            _record_history(
                connection,
                task_id=task_id,
                field_name="cancellation",
                old_value="ACTIVE",
                new_value="CANCELLED",
                actor=actor,
                task=task,
                reason=cleaned_reason,
                changed_at=now,
            )
            for row in rows:
                _notify(
                    connection,
                    event_type=EVENT_TASK_CANCELLED,
                    task_id=task_id,
                    recipient_person_id=int(row["person_id"]),
                    created_at=now,
                )
    except TaskAuthorizationError as error:
        _deny_and_raise(
            connection,
            actor_person_id,
            operation=OPERATION_TASK_CANCEL,
            subject_id=task_id,
            reason=error.reason,
        )
