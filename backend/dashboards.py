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

Lists are capped by the configured dashboard list limit and each payload
reports a ``has_more`` flag per list, so a large festival cannot turn one
dashboard response into an unbounded read; the count badges carry the true
totals. ``/tasks`` and the Admin ledgers keep their own configured page caps
(NFR-REL-06).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backend import approvals, notification_delivery, orchestration, system_control, visibility
from backend import tasks as task_service
from backend.approvals import ApprovalView
from backend.auth import AuthenticatedPerson
from backend.notification_delivery import NotificationView
from backend.orchestration import GroupBacklog
from backend.relevance import (
    STAGE_RELEVANCE_CLASSIFICATION,
    STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
)
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
    counts: tuple[tuple[str, int], ...]
    attention: tuple[tuple[str, int], ...]
    has_more: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class AHDashboard:
    """FR-UI-03: review queue, created tasks with progress, people, approvals."""

    review_queue: tuple[ReviewQueueItem, ...]
    created_tasks: tuple[TaskProgressView, ...]
    volunteers: tuple[VisiblePerson, ...]
    approvals: tuple[ApprovalView, ...]
    counts: tuple[tuple[str, int], ...]
    attention: tuple[tuple[str, int], ...]
    has_more: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class HeadDashboard:
    """FR-UI-04: the vertical's tasks, people, approvals and cross-vertical work."""

    review_queue: tuple[ReviewQueueItem, ...]
    tasks: tuple[TaskProgressView, ...]
    people: tuple[VisiblePerson, ...]
    approvals: tuple[ApprovalView, ...]
    cross_vertical_tasks: tuple[TaskSummary, ...]
    counts: tuple[tuple[str, int], ...]
    attention: tuple[tuple[str, int], ...]
    has_more: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class BacklogSummary:
    """NFR-REL-06: unprocessed count and the age of the oldest bucket."""

    unprocessed_buckets: int
    unassigned_messages: int
    oldest_age_seconds: int | None
    groups: tuple[GroupBacklog, ...]


@dataclass(frozen=True)
class VerticalTaskCounts:
    """One vertical's task total and per-status split (FR-UI-05 glance)."""

    vertical_id: int
    total: int
    by_status: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class AdminDashboard:
    """FR-UI-05 festival-at-a-glance counts (owner decision B, Phase 12 D1)."""

    messages_total: int
    messages_last_24h: int
    buckets_by_state: tuple[tuple[str, int], ...]
    relevance_relevant: int
    relevance_irrelevant: int
    relevance_failed: int
    relevance_unclassified: int
    proposals_pending_review: int
    proposals_accepted: int
    proposals_rejected: int
    tasks_total: int
    tasks_by_status: tuple[tuple[str, int], ...]
    tasks_by_vertical: tuple[VerticalTaskCounts, ...]
    notifications_pending: int
    notifications_delivered: int
    notifications_retrying: int
    notifications_exhausted: int
    notifications_no_endpoint: int
    backlog_unprocessed_buckets: int
    backlog_oldest_age_seconds: int | None
    processing_paused: bool
    delivery_paused: bool


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


def _require_limit(limit: int) -> int:
    """A dashboard list cap must be a positive integer (owner decision E)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    return limit


def _cap[Item](
    items: tuple[Item, ...], limit: int
) -> tuple[tuple[Item, ...], bool]:
    """Return at most ``limit`` items and whether more were dropped."""
    return items[:limit], len(items) > limit


def _task_needs_work(accepted_count: int, completed_count: int) -> bool:
    """A task still needs someone while it has no accepted work or unfinished work."""
    return accepted_count == 0 or completed_count < accepted_count


def _progress_needs_work(view: TaskProgressView) -> bool:
    """The same reading over a progress roll-up's live accepted assignees."""
    live = [
        assignee
        for assignee in view.assignees
        if assignee.agreement == "ACCEPTED" and not assignee.cancelled
    ]
    return not live or any(assignee.work_status != "COMPLETED" for assignee in live)


def volunteer_dashboard(
    connection: sqlite3.Connection,
    *,
    actor: AuthenticatedPerson,
    limit: int,
) -> VolunteerDashboard:
    """FR-UI-02: split the visible tasks into own and claimable, add notices."""
    _require_limit(limit)
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
    tasks_page, tasks_more = _cap(own, limit)
    open_page, open_more = _cap(open_tasks, limit)
    notification_page, notifications_more = _cap(notifications, limit)
    return VolunteerDashboard(
        tasks=tasks_page,
        open_tasks=open_page,
        notifications=notification_page,
        counts=(
            ("tasks", len(own)),
            ("open_tasks", len(open_tasks)),
            ("notifications", len(notifications)),
        ),
        attention=(
            (
                "unread_notifications",
                sum(1 for item in notifications if item.read_at is None),
            ),
            ("open_tasks", len(open_tasks)),
            (
                "active_tasks",
                sum(
                    1
                    for task in own
                    if _task_needs_work(
                        task.accepted_count, task.completed_count
                    )
                ),
            ),
        ),
        has_more=(
            ("tasks", tasks_more),
            ("open_tasks", open_more),
            ("notifications", notifications_more),
        ),
    )


def ah_dashboard(
    connection: sqlite3.Connection,
    *,
    actor: AuthenticatedPerson,
    limit: int,
) -> AHDashboard:
    """FR-UI-03: the AH's review queue, created tasks, reports and approvals."""
    _require_limit(limit)
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
    review_queue = _review_queue(connection, actor.person_id)
    pending_approvals = approvals.list_pending_approvals(
        connection, actor_person_id=actor.person_id
    )
    review_page, review_more = _cap(review_queue, limit)
    created_page, created_more = _cap(created, limit)
    volunteer_page, volunteers_more = _cap(volunteers, limit)
    approval_page, approvals_more = _cap(pending_approvals, limit)
    return AHDashboard(
        review_queue=review_page,
        created_tasks=created_page,
        volunteers=volunteer_page,
        approvals=approval_page,
        counts=(
            ("review_queue", len(review_queue)),
            ("created_tasks", len(created)),
            ("volunteers", len(volunteers)),
            ("approvals", len(pending_approvals)),
        ),
        attention=(
            ("pending_review", len(review_queue)),
            ("approvals", len(pending_approvals)),
            (
                "active_created_tasks",
                sum(1 for item in created if _progress_needs_work(item)),
            ),
        ),
        has_more=(
            ("review_queue", review_more),
            ("created_tasks", created_more),
            ("volunteers", volunteers_more),
            ("approvals", approvals_more),
        ),
    )


def head_dashboard(
    connection: sqlite3.Connection,
    *,
    actor: AuthenticatedPerson,
    limit: int,
) -> HeadDashboard:
    """FR-UI-04: the vertical's tasks, people, approvals and cross-vertical work."""
    _require_limit(limit)
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
    review_queue = _review_queue(connection, actor.person_id)
    pending_approvals = approvals.list_pending_approvals(
        connection, actor_person_id=actor.person_id
    )
    review_page, review_more = _cap(review_queue, limit)
    tasks_page, tasks_more = _cap(progress_views, limit)
    people_page, people_more = _cap(people, limit)
    approval_page, approvals_more = _cap(pending_approvals, limit)
    cross_page, cross_more = _cap(cross_vertical, limit)
    return HeadDashboard(
        review_queue=review_page,
        tasks=tasks_page,
        people=people_page,
        approvals=approval_page,
        cross_vertical_tasks=cross_page,
        counts=(
            ("review_queue", len(review_queue)),
            ("tasks", len(progress_views)),
            ("people", len(people)),
            ("approvals", len(pending_approvals)),
            ("cross_vertical_tasks", len(cross_vertical)),
        ),
        attention=(
            ("pending_review", len(review_queue)),
            ("approvals", len(pending_approvals)),
            (
                "active_tasks",
                sum(
                    1
                    for item in progress_views
                    if _progress_needs_work(item)
                ),
            ),
        ),
        has_more=(
            ("review_queue", review_more),
            ("tasks", tasks_more),
            ("people", people_more),
            ("approvals", approvals_more),
            ("cross_vertical_tasks", cross_more),
        ),
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


#: Task-status vocabulary, in the order the payload reports it (FR-TSK-02).
_TASK_STATUSES: tuple[str, ...] = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED")

#: Bucket processing states (0003's CHECK), in report order.
_BUCKET_STATES: tuple[str, ...] = (
    "OPEN",
    "CLOSED",
    "PROCESSED",
    "FAILED",
    "AWAITING_RECOVERY",
)

#: The two relevance-stage tokens whose failed attempts are reported.
_RELEVANCE_STAGES: tuple[str, ...] = (
    STAGE_RELEVANCE_CLASSIFICATION,
    STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
)


def _count(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return 0 if row is None else int(row["n"])


def _task_counts(
    connection: sqlite3.Connection,
) -> tuple[
    int,
    tuple[tuple[str, int], ...],
    tuple[VerticalTaskCounts, ...],
]:
    """Derive every task's status from its live accepted assignments.

    A task with no accepted, non-cancelled assignment is ``NOT_STARTED``; when
    every such assignment is ``COMPLETED`` the task is ``COMPLETED``; any
    ``IN_PROGRESS`` or ``COMPLETED`` assignment makes it ``IN_PROGRESS``. This
    is a reading of the existing assignee records, not a stored task state.
    """
    rows = connection.execute(
        "SELECT id, COALESCE(responsible_vertical_id, source_vertical_id)"
        " AS vertical_id FROM task ORDER BY id"
    ).fetchall()
    statuses: dict[int, set[str]] = {int(row["id"]): set() for row in rows}
    for row in connection.execute(
        "SELECT task_id, work_status FROM task_assignee"
        " WHERE cancelled = 0 AND agreement = 'ACCEPTED'"
    ).fetchall():
        statuses[int(row["task_id"])].add(str(row["work_status"]))

    def derive(task_id: int) -> str:
        work = statuses[task_id]
        if work and work == {"COMPLETED"}:
            return "COMPLETED"
        if work & {"IN_PROGRESS", "COMPLETED"}:
            return "IN_PROGRESS"
        return "NOT_STARTED"

    totals = dict.fromkeys(_TASK_STATUSES, 0)
    per_vertical: dict[int, dict[str, int]] = {}
    for row in rows:
        status = derive(int(row["id"]))
        totals[status] += 1
        counts = per_vertical.setdefault(
            int(row["vertical_id"]), dict.fromkeys(_TASK_STATUSES, 0)
        )
        counts[status] += 1

    return (
        len(rows),
        tuple((status, totals[status]) for status in _TASK_STATUSES),
        tuple(
            VerticalTaskCounts(
                vertical_id=vertical_id,
                total=sum(counts.values()),
                by_status=tuple(
                    (status, counts[status]) for status in _TASK_STATUSES
                ),
            )
            for vertical_id, counts in sorted(per_vertical.items())
        ),
    )


def admin_dashboard(
    connection: sqlite3.Connection,
    *,
    now: str,
) -> AdminDashboard:
    """FR-UI-05: cumulative festival counts and the current backlog reading.

    Owner decision B (Phase 12 D1): stop/resume, exports and counts at a
    glance. Attempt/denial/evaluation ledgers and configuration names left
    this payload; their Admin routes remain. ``last_24h`` compares SQLite's
    ``julianday`` so the stored timestamps are read as instants; an unparsable
    value counts as not recent rather than failing the dashboard.
    """
    messages_total = _count(connection, "SELECT COUNT(*) AS n FROM source_message")
    messages_last_24h = _count(
        connection,
        "SELECT COUNT(*) AS n FROM source_message"
        " WHERE julianday(received_at) >= julianday(?) - 1.0",
        (now,),
    )

    bucket_counts = dict.fromkeys(_BUCKET_STATES, 0)
    for row in connection.execute(
        "SELECT processing_state, COUNT(*) AS n FROM bucket"
        " GROUP BY processing_state"
    ).fetchall():
        state = str(row["processing_state"])
        if state in bucket_counts:
            bucket_counts[state] = int(row["n"])

    relevance_relevant = _count(
        connection,
        "SELECT COUNT(DISTINCT message_id) AS n FROM relevance_decision"
        " WHERE decision = 'RELEVANT'",
    )
    relevance_irrelevant = _count(
        connection,
        "SELECT COUNT(DISTINCT message_id) AS n FROM relevance_decision"
        " WHERE decision = 'IRRELEVANT'",
    )
    placeholders = ", ".join("?" for _ in _RELEVANCE_STAGES)
    relevance_failed = _count(
        connection,
        "SELECT COUNT(*) AS n FROM processing_attempt"
        f" WHERE stage IN ({placeholders}) AND outcome != 'SUCCEEDED'",
        _RELEVANCE_STAGES,
    )
    relevance_unclassified = _count(
        connection,
        "SELECT COUNT(*) AS n FROM source_message WHERE flag_unclassified = 1",
    )

    proposals_pending_review = _count(
        connection,
        "SELECT COUNT(*) AS n FROM proposal"
        " WHERE review_state IN ('PENDING', 'EDITED')",
    )
    proposals_accepted = _count(
        connection,
        "SELECT COUNT(*) AS n FROM proposal WHERE review_state = 'ACCEPTED'",
    )
    proposals_rejected = _count(
        connection,
        "SELECT COUNT(*) AS n FROM proposal WHERE review_state = 'REJECTED'",
    )

    tasks_total, tasks_by_status, tasks_by_vertical = _task_counts(connection)

    notification_counts = dict.fromkeys(
        ("PENDING", "SENDING", "SENT", "FAILED", "EXHAUSTED"), 0
    )
    for row in connection.execute(
        "SELECT delivery_state, COUNT(*) AS n FROM notification"
        " GROUP BY delivery_state"
    ).fetchall():
        state = str(row["delivery_state"])
        if state in notification_counts:
            notification_counts[state] = int(row["n"])
    notifications_no_endpoint = _count(
        connection,
        "SELECT COUNT(*) AS n FROM notification AS n"
        " WHERE n.delivery_state = 'EXHAUSTED'"
        " AND (SELECT a.outcome FROM notification_attempt AS a"
        " WHERE a.notification_id = n.id"
        " ORDER BY a.attempt_number DESC LIMIT 1) = 'NO_ENDPOINT'",
    )

    backlog = backlog_summary(connection)
    switches = system_control.read_state(connection)

    return AdminDashboard(
        messages_total=messages_total,
        messages_last_24h=messages_last_24h,
        buckets_by_state=tuple(
            (state, bucket_counts[state]) for state in _BUCKET_STATES
        ),
        relevance_relevant=relevance_relevant,
        relevance_irrelevant=relevance_irrelevant,
        relevance_failed=relevance_failed,
        relevance_unclassified=relevance_unclassified,
        proposals_pending_review=proposals_pending_review,
        proposals_accepted=proposals_accepted,
        proposals_rejected=proposals_rejected,
        tasks_total=tasks_total,
        tasks_by_status=tasks_by_status,
        tasks_by_vertical=tasks_by_vertical,
        notifications_pending=(
            notification_counts["PENDING"] + notification_counts["SENDING"]
        ),
        notifications_delivered=notification_counts["SENT"],
        notifications_retrying=notification_counts["FAILED"],
        notifications_exhausted=(
            notification_counts["EXHAUSTED"] - notifications_no_endpoint
        ),
        notifications_no_endpoint=notifications_no_endpoint,
        backlog_unprocessed_buckets=backlog.unprocessed_buckets,
        backlog_oldest_age_seconds=backlog.oldest_age_seconds,
        processing_paused=switches.processing_paused,
        delivery_paused=switches.delivery_paused,
    )
