"""Dashboard read composition (W6; FR-UI-02..05, NFR-SEC-03, NFR-REL-06).

Each function here composes read-only domain services into one role's
dashboard. Nothing writes, opens an outer transaction or holds state between
requests, and every task list is the same ``visible_tasks`` /
``task_progress`` pair the task routes use, so a dashboard can never show a
task the role could not read directly (FR-TSK-11, NFR-SEC-03).

People appear only when :func:`backend.visibility.visible_fields` returns
fields for them; the serializers then emit exactly those fields, so a hidden
phone number is absent rather than ``null``. Exception (a) — a task's creator
sees its assignees' contact details — is applied by passing ``task_id`` to
``visible_fields`` for the assignee rows of a task.

Lists are unpaginated: these are one-screen MVP dashboards over a single
festival's data, and silently truncating them would hide work. ``/tasks`` and
the Admin ledgers keep their configured page caps (NFR-REL-06).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backend import approvals, notification_delivery, orchestration, review, visibility
from backend import tasks as task_service
from backend.approvals import ApprovalView
from backend.auth import AuthenticatedPerson
from backend.config import Config
from backend.notification_delivery import NotificationView
from backend.orchestration import GroupBacklog
from backend.tasks import TaskSummary
from backend.visibility import PersonRecord


@dataclass(frozen=True)
class ReviewQueueItem:
    """One pending proposal assigned to the caller as reviewer (no content)."""

    proposal_id: int
    ptype: str
    description: str
    source_vertical_id: int
    generated_at: str


@dataclass(frozen=True)
class VisiblePerson:
    """One person plus the exact fields the caller may see."""

    person: PersonRecord
    fields: frozenset[str]


@dataclass(frozen=True)
class AssigneeView:
    """One assignment row with the assignee's permitted contact fields."""

    person_id: int
    agreement: str
    work_status: str
    cancelled: bool
    person: PersonRecord
    fields: frozenset[str]


@dataclass(frozen=True)
class TaskProgressView:
    """One task with its per-assignee progress (FR-TSK-07)."""

    task: TaskSummary
    assignees: tuple[AssigneeView, ...]


@dataclass(frozen=True)
class VolunteerDashboard:
    """FR-UI-02: own tasks, open tasks in own verticals, notifications."""

    tasks: tuple[TaskSummary, ...]
    open_tasks: tuple[TaskSummary, ...]
    notifications: tuple[NotificationView, ...]


@dataclass(frozen=True)
class AHDashboard:
    """FR-UI-03: review queue, created tasks with progress, people, approvals."""

    review_queue: tuple[ReviewQueueItem, ...]
    created_tasks: tuple[TaskProgressView, ...]
    volunteers: tuple[VisiblePerson, ...]
    approvals: tuple[ApprovalView, ...]


@dataclass(frozen=True)
class HeadDashboard:
    """FR-UI-04: the vertical's tasks, people, approvals and cross-vertical work."""

    review_queue: tuple[ReviewQueueItem, ...]
    tasks: tuple[TaskProgressView, ...]
    people: tuple[VisiblePerson, ...]
    approvals: tuple[ApprovalView, ...]
    cross_vertical_tasks: tuple[TaskSummary, ...]


@dataclass(frozen=True)
class BacklogSummary:
    """NFR-REL-06: unprocessed count and the age of the oldest bucket."""

    unprocessed_buckets: int
    unassigned_messages: int
    oldest_age_seconds: int | None
    groups: tuple[GroupBacklog, ...]


@dataclass(frozen=True)
class AdminDashboard:
    """FR-UI-05: backlog, attempts, denials, evaluation and config names."""

    backlog: BacklogSummary
    attempts_total: int
    attempts_by_outcome: tuple[tuple[str, int], ...]
    denials_total: int
    evaluation_labelled: int
    config_keys: tuple[str, ...]


def _vertical_ids(
    connection: sqlite3.Connection, person_id: int
) -> frozenset[int]:
    rows = connection.execute(
        "SELECT vertical_id FROM person_vertical WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    return frozenset(int(row["vertical_id"]) for row in rows)


def _visible_task_ids(
    connection: sqlite3.Connection, actor: AuthenticatedPerson
) -> tuple[TaskSummary, ...]:
    return task_service.visible_tasks(
        connection, viewer_person_id=actor.person_id
    )


def _person_view(
    connection: sqlite3.Connection,
    actor: AuthenticatedPerson,
    person_id: int,
    *,
    task_id: int | None = None,
) -> VisiblePerson | None:
    fields = visibility.visible_fields(
        connection, actor, person_id, task_id=task_id
    )
    if not fields:
        return None
    record = visibility.person_record(connection, person_id)
    if record is None:  # pragma: no cover - the FK and the field check agree
        return None
    return VisiblePerson(person=record, fields=fields)


def _people(
    connection: sqlite3.Connection,
    actor: AuthenticatedPerson,
    sql: str,
    params: tuple[object, ...],
) -> tuple[VisiblePerson, ...]:
    views: list[VisiblePerson] = []
    for row in connection.execute(sql, params).fetchall():
        view = _person_view(connection, actor, int(row["id"]))
        if view is not None:
            views.append(view)
    return tuple(views)


def _task_progress_view(
    connection: sqlite3.Connection, actor: AuthenticatedPerson, task: TaskSummary
) -> TaskProgressView:
    progress = task_service.task_progress(
        connection, task.id, viewer_person_id=actor.person_id
    )
    assignees: list[AssigneeView] = []
    for assignee in progress.assignees:
        fields = visibility.visible_fields(
            connection, actor, assignee.person_id, task_id=task.id
        )
        if not fields:
            continue
        record = visibility.person_record(connection, assignee.person_id)
        if record is None:  # pragma: no cover - the JOIN guarantees the row
            continue
        assignees.append(
            AssigneeView(
                person_id=assignee.person_id,
                agreement=assignee.agreement,
                work_status=assignee.work_status,
                cancelled=assignee.cancelled,
                person=record,
                fields=fields,
            )
        )
    return TaskProgressView(task=task, assignees=tuple(assignees))


def _review_queue(
    connection: sqlite3.Connection, person_id: int
) -> tuple[ReviewQueueItem, ...]:
    """Pending proposals whose reviewer (the primary sender) is ``person_id``.

    Phase 6 routes review by the primary message's sender, so this selects the
    same rows the review route would authorize, without content.
    """
    rows = connection.execute(
        "SELECT p.id AS proposal_id, p.ptype, p.description,"
        " p.source_vertical_id, p.generated_at"
        " FROM proposal AS p"
        " JOIN source_message AS m ON m.id = p.primary_message_id"
        " WHERE p.review_state = 'PENDING' AND m.sender_person_id = ?"
        " ORDER BY p.id",
        (person_id,),
    ).fetchall()
    return tuple(
        ReviewQueueItem(
            proposal_id=int(row["proposal_id"]),
            ptype=str(row["ptype"]),
            description="" if row["description"] is None else str(row["description"]),
            source_vertical_id=int(row["source_vertical_id"]),
            generated_at=str(row["generated_at"]),
        )
        for row in rows
    )


def volunteer_dashboard(
    connection: sqlite3.Connection, *, actor: AuthenticatedPerson
) -> VolunteerDashboard:
    """FR-UI-02: split the visible tasks into own and claimable, add notices."""
    visible = _visible_task_ids(connection, actor)
    own_ids = {
        int(row["task_id"])
        for row in connection.execute(
            "SELECT task_id FROM task_assignee WHERE person_id = ?"
            " AND agreement != 'DENIED' AND cancelled = 0",
            (actor.person_id,),
        ).fetchall()
    }
    verticals = _vertical_ids(connection, actor.person_id)
    own = tuple(task for task in visible if task.id in own_ids)
    open_tasks = tuple(
        task
        for task in visible
        if task.assignment_mode == "OPEN"
        and task.responsible_vertical_id in verticals
    )
    notifications = notification_delivery.list_notifications(
        connection, person_id=actor.person_id
    )
    return VolunteerDashboard(
        tasks=own, open_tasks=open_tasks, notifications=notifications
    )


def ah_dashboard(
    connection: sqlite3.Connection, *, actor: AuthenticatedPerson
) -> AHDashboard:
    """FR-UI-03: the AH's review queue, created tasks, reports and approvals."""
    visible = _visible_task_ids(connection, actor)
    created = tuple(
        _task_progress_view(connection, actor, task)
        for task in visible
        if task.creator_id == actor.person_id
    )
    volunteers = _people(
        connection,
        actor,
        "SELECT p.id FROM person AS p WHERE p.active = 1"
        " AND p.reports_to = ? AND lower(trim(p.role)) = 'volunteer'"
        " ORDER BY p.id",
        (actor.person_id,),
    )
    return AHDashboard(
        review_queue=_review_queue(connection, actor.person_id),
        created_tasks=created,
        volunteers=volunteers,
        approvals=approvals.list_pending_approvals(
            connection, actor_person_id=actor.person_id
        ),
    )


def head_dashboard(
    connection: sqlite3.Connection, *, actor: AuthenticatedPerson
) -> HeadDashboard:
    """FR-UI-04: the vertical's tasks, people, approvals and cross-vertical work."""
    visible = _visible_task_ids(connection, actor)
    progress_views = tuple(
        _task_progress_view(connection, actor, task) for task in visible
    )
    verticals = _vertical_ids(connection, actor.person_id)
    people = _people(
        connection,
        actor,
        "SELECT DISTINCT p.id FROM person AS p"
        " JOIN person_vertical AS pv ON pv.person_id = p.id"
        " WHERE p.active = 1 AND p.id != ? AND pv.vertical_id IN ("
        " SELECT vertical_id FROM person_vertical WHERE person_id = ?)"
        " ORDER BY p.id",
        (actor.person_id, actor.person_id),
    )
    cross_vertical = tuple(
        task
        for task in visible
        if task.task_type == "CROSS_VERTICAL"
        and (
            task.source_vertical_id in verticals
            or task.responsible_vertical_id in verticals
        )
    )
    return HeadDashboard(
        review_queue=_review_queue(connection, actor.person_id),
        tasks=progress_views,
        people=people,
        approvals=approvals.list_pending_approvals(
            connection, actor_person_id=actor.person_id
        ),
        cross_vertical_tasks=cross_vertical,
    )


def backlog_summary(connection: sqlite3.Connection) -> BacklogSummary:
    """NFR-REL-06: one unprocessed count and the oldest bucket's age.

    The age is the largest group age because the oldest unprocessed bucket is
    the one furthest behind accepted progress; an undatable group contributes
    no age, exactly as the orchestration reading reports it.
    """
    pipeline = orchestration.pipeline_backlog(connection)
    ages = [
        group.age_event_seconds
        for group in pipeline.groups
        if group.age_event_seconds is not None
    ]
    return BacklogSummary(
        unprocessed_buckets=pipeline.unprocessed_buckets,
        unassigned_messages=pipeline.unassigned_messages,
        oldest_age_seconds=max(ages) if ages else None,
        groups=pipeline.groups,
    )


def admin_dashboard(
    connection: sqlite3.Connection,
    *,
    actor: AuthenticatedPerson,
    config: Config,
) -> AdminDashboard:
    """FR-UI-05: backlog, attempt and denial counts, evaluation, config names.

    Evaluation availability is measured by calling the same Admin-only export
    reader the route uses, so "available" reflects real data and authority
    rather than a constant. Configuration contributes key names only.
    """
    attempts = connection.execute(
        "SELECT outcome, COUNT(*) AS n FROM processing_attempt"
        " GROUP BY outcome ORDER BY outcome"
    ).fetchall()
    denials = connection.execute(
        "SELECT COUNT(*) AS n FROM authorization_denial"
    ).fetchone()
    labelled = review.list_reviewed_proposals_for_eval(
        connection, actor_person_id=actor.person_id, config=config
    )
    return AdminDashboard(
        backlog=backlog_summary(connection),
        attempts_total=sum(int(row["n"]) for row in attempts),
        attempts_by_outcome=tuple(
            (str(row["outcome"]), int(row["n"])) for row in attempts
        ),
        denials_total=0 if denials is None else int(denials["n"]),
        evaluation_labelled=len(labelled),
        config_keys=tuple(sorted(config.as_public_dict())),
    )
