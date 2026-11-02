"""Task service tests (Phase 7 W5/W6; FR-TSK-01..16, NFR-CON-02, NFR-AUD-03).

W5 covers the single visibility filter and read model (plan §4.5): hidden
``PENDING``/``DENIED`` tasks never appear, cancelled tasks are opt-in, scope is
derived inside the service from the current roster (never passed in), and
``task_progress`` enforces the matrix's progress row. W6 appends the lifecycle
writes below the read-model block.

Synthetic roster only; every phone/roll value is obviously fake.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.tasks import (
    REASON_NOT_ADMIN,
    REASON_NOT_ASSIGNEE,
    REASON_NOT_TASK_VIEWER,
    InvalidAgreementError,
    StaleTaskVersionError,
    TaskAssigneeProgress,
    TaskAuthorizationError,
    TaskCapacityError,
    TaskEdit,
    TaskNotFoundError,
    TaskProgress,
    TaskStateError,
    TaskSummary,
    _live_task_row,
    cancel_task,
    claim_open_task,
    edit_task,
    reassign_task,
    set_agreement,
    task_progress,
    update_work_status,
    visible_tasks,
)

# --- scenario constants ---------------------------------------------------

AH = 1
VOLUNTEER = 2
VOLUNTEER2 = 3
HEAD = 4
ADMIN = 5
AH_V2 = 6
HEAD_V2 = 7
VOL_V2 = 8
VOL_V3 = 9
INACTIVE = 10
OTHER_AH = 11
VOL_A = 12
VOL_B = 13
VOL_C = 14
VOL_D = 15
VOL_E = 16

_GENERATED_AT = "2026-11-01T09:59:00+00:00"
_TASK_AT = "2026-11-01T09:59:30+00:00"


def _vertical(connection: sqlite3.Connection, vertical_id: int, name: str) -> None:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name)
    )


def _person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    role: str,
    vertical_id: int,
    *,
    active: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            person_id,
            name,
            f"+919833330{person_id:03d}",
            role,
            active,
            f"ROLL-3{person_id:02d}",
        ),
    )
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )


def _seed_world(connection: sqlite3.Connection) -> None:
    _vertical(connection, 1, "PR")
    _vertical(connection, 2, "Transport")
    _vertical(connection, 3, "Design")

    _person(connection, AH, "Aarav", "AH", 1)
    _person(connection, VOLUNTEER, "Neha", "Volunteer", 1)
    _person(connection, VOLUNTEER2, "Isha", "Volunteer", 1)
    _person(connection, HEAD, "Vikram", "Head", 1)
    _person(connection, ADMIN, "Diya", "Admin", 1)
    _person(connection, AH_V2, "Kabir", "AH", 2)
    _person(connection, HEAD_V2, "Meera", "Head", 2)
    _person(connection, VOL_V2, "Ravi", "Volunteer", 2)
    _person(connection, VOL_V3, "Tara", "Volunteer", 3)
    _person(connection, INACTIVE, "Dev", "Volunteer", 1, active=0)
    _person(connection, OTHER_AH, "Sana", "AH", 1)
    _person(connection, VOL_A, "Aditi", "Volunteer", 1)
    _person(connection, VOL_B, "Rohit", "Volunteer", 1)
    _person(connection, VOL_C, "Priya", "Volunteer", 1)
    _person(connection, VOL_D, "Karan", "Volunteer", 1)
    _person(connection, VOL_E, "Maya", "Volunteer", 1)

    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (2, 'chan-2', 2)"
    )
    connection.execute(
        "INSERT INTO bucket (id, group_id, start_event_timestamp,"
        " end_event_timestamp) VALUES (1, 1, '2026-11-01T09:58:00+00:00',"
        " '2026-11-01T09:58:00+00:00')"
    )


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    apply_migrations(connection)
    _seed_world(connection)
    yield connection
    connection.close()


@pytest.fixture()
def file_db(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """A file-backed database so two threads can share the same store."""
    path = tmp_path / "tasks.db"
    connection = connect(path)
    apply_migrations(connection)
    _seed_world(connection)
    yield connection, path
    connection.close()


def _make_task(
    connection: sqlite3.Connection,
    *,
    creator_id: int = AH,
    source_vertical_id: int = 1,
    responsible_vertical_id: int | None = None,
    task_type: str | None = None,
    assignment_mode: str = "EXPLICIT",
    people_needed: int | None = None,
    people_needed_unlimited: int = 0,
    approval_state: str = "NOT_REQUIRED",
    cancellation: str = "ACTIVE",
    description: str = "Arrange the generator",
) -> int:
    """Insert one message, attempt, proposal and task; return the task id."""
    if responsible_vertical_id is None:
        responsible_vertical_id = source_vertical_id
    if task_type is None:
        task_type = (
            "INTERNAL"
            if source_vertical_id == responsible_vertical_id
            else "CROSS_VERTICAL"
        )
    message_id = int(
        connection.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM source_message"
        ).fetchone()["n"]
    )
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (?, 'discord', ?, 1, ?, '2026-11-01T09:58:00+00:00',"
        " '2026-11-01T09:58:00+00:00', 'primary line')",
        (message_id, f"msg-{message_id}", creator_id),
    )
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (1, ?, 1)",
        (message_id,),
    )
    attempt_id = int(
        connection.execute(
            "INSERT INTO processing_attempt (stage, bucket_id, started_at, outcome)"
            " VALUES ('proposal_generation', 1, ?, 'SUCCEEDED')",
            (_GENERATED_AT,),
        ).lastrowid
        or 0
    )
    proposal_id = int(
        connection.execute(
            "INSERT INTO proposal (attempt_id, bucket_id, primary_message_id,"
            " source_vertical_id, ptype, assignment_mode, task_type,"
            " people_needed, people_needed_unlimited, structurally_valid,"
            " generated_at, model_id, model_version, prompt_version)"
            " VALUES (?, 1, ?, ?, 'TASK', ?, ?, ?, ?, 1, ?, 'model-p', '1.0.0',"
            " 'prompt-1')",
            (
                attempt_id,
                message_id,
                source_vertical_id,
                assignment_mode,
                task_type,
                people_needed,
                people_needed_unlimited,
                _GENERATED_AT,
            ),
        ).lastrowid
        or 0
    )
    task_id = int(
        connection.execute(
            "INSERT INTO task (proposal_id, task_type, source_vertical_id,"
            " responsible_vertical_id, creator_id, description, assignment_mode,"
            " people_needed, people_needed_unlimited, approval_state,"
            " cancellation, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                task_type,
                source_vertical_id,
                responsible_vertical_id,
                creator_id,
                description,
                assignment_mode,
                people_needed,
                people_needed_unlimited,
                approval_state,
                cancellation,
                _TASK_AT,
            ),
        ).lastrowid
        or 0
    )
    return task_id


def _assign(
    connection: sqlite3.Connection,
    task_id: int,
    person_id: int,
    *,
    agreement: str = "AWAITING",
    work_status: str = "NOT_STARTED",
    cancelled: int = 0,
) -> int:
    agreed_at = None if agreement == "AWAITING" else _TASK_AT
    return int(
        connection.execute(
            "INSERT INTO task_assignee (task_id, person_id, agreement, agreed_at,"
            " work_status, cancelled, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, person_id, agreement, agreed_at, work_status, cancelled, _TASK_AT),
        ).lastrowid
        or 0
    )


def _task_ids(summaries: tuple[TaskSummary, ...]) -> list[int]:
    return [summary.id for summary in summaries]


def _denials(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM authorization_denial ORDER BY id").fetchall()


def _history(connection: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM task_history WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def _notifications(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM notification ORDER BY id").fetchall()


def _assignee_rows(
    connection: sqlite3.Connection, task_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM task_assignee WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def _summary_of(
    connection: sqlite3.Connection,
    task_id: int,
    *,
    viewer_person_id: int = ADMIN,
    include_cancelled: bool = False,
) -> TaskSummary:
    return next(
        summary
        for summary in visible_tasks(
            connection,
            viewer_person_id=viewer_person_id,
            include_cancelled=include_cancelled,
        )
        if summary.id == task_id
    )


# =========================================================================
# W5 — visibility filter
# =========================================================================


def test_visible_tasks_hides_pending_and_denied(conn: sqlite3.Connection) -> None:
    """FR-PRP-16/NFR-SEC-03: a hidden task is never listed, even for Admin."""
    live = _make_task(conn)
    pending = _make_task(conn, approval_state="PENDING")
    denied = _make_task(conn, approval_state="DENIED")

    assert _task_ids(visible_tasks(conn, viewer_person_id=ADMIN)) == [live]
    assert _task_ids(
        visible_tasks(conn, viewer_person_id=ADMIN, include_hidden=True)
    ) == [live, pending, denied]
    assert pending != denied


def test_live_task_row_treats_hidden_and_cancelled_as_not_found(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.5: the shared loader hides PENDING/DENIED/CANCELLED rows."""
    live = _make_task(conn)
    pending = _make_task(conn, approval_state="PENDING")
    denied = _make_task(conn, approval_state="DENIED")
    cancelled = _make_task(conn, cancellation="CANCELLED")

    assert _live_task_row(conn, live) is not None
    assert _live_task_row(conn, pending) is None
    assert _live_task_row(conn, denied) is None
    assert _live_task_row(conn, cancelled) is None
    assert _live_task_row(conn, 9999) is None


def test_visible_tasks_hides_cancelled_unless_requested(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-08: a cancelled task leaves the live list but stays queryable."""
    live = _make_task(conn)
    cancelled = _make_task(conn, cancellation="CANCELLED")

    assert _task_ids(visible_tasks(conn, viewer_person_id=ADMIN)) == [live]
    assert _task_ids(
        visible_tasks(conn, viewer_person_id=ADMIN, include_cancelled=True)
    ) == [live, cancelled]
    # Hidden still beats include_cancelled: a PENDING task never appears.
    _make_task(conn, approval_state="PENDING", cancellation="CANCELLED")
    assert _task_ids(
        visible_tasks(conn, viewer_person_id=ADMIN, include_cancelled=True)
    ) == [live, cancelled]


def test_include_hidden_requires_a_current_admin(
    conn: sqlite3.Connection,
) -> None:
    """NFR-SEC-03(c): only Admin may widen the filter, and the denial is recorded."""
    _make_task(conn, approval_state="PENDING")

    with pytest.raises(TaskAuthorizationError) as excinfo:
        visible_tasks(conn, viewer_person_id=HEAD, include_hidden=True)

    assert excinfo.value.reason == REASON_NOT_ADMIN
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == HEAD
    assert str(denial["reason"]) == REASON_NOT_ADMIN


def test_visible_tasks_records_missing_and_inactive_viewers(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-01/02/05/06: authorization reads the current roster."""
    with pytest.raises(TaskAuthorizationError) as excinfo:
        visible_tasks(conn, viewer_person_id=999)

    assert excinfo.value.reason == "ACTOR_NOT_FOUND"
    with pytest.raises(TaskAuthorizationError) as excinfo:
        visible_tasks(conn, viewer_person_id=INACTIVE)

    assert excinfo.value.reason == "ACTOR_INACTIVE"
    denials = _denials(conn)
    assert len(denials) == 2
    assert denials[0]["actor_person_id"] is None
    assert int(denials[1]["actor_person_id"]) == INACTIVE


def test_visible_tasks_admin_scope_is_every_live_task(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-16/FR-AUT-04: Admin sees all live tasks across verticals."""
    first = _make_task(conn, source_vertical_id=1, responsible_vertical_id=1)
    second = _make_task(
        conn,
        creator_id=AH_V2,
        source_vertical_id=2,
        responsible_vertical_id=2,
    )
    third = _make_task(
        conn,
        creator_id=AH_V2,
        source_vertical_id=2,
        responsible_vertical_id=1,
    )

    assert _task_ids(visible_tasks(conn, viewer_person_id=ADMIN)) == [
        first,
        second,
        third,
    ]


def test_visible_tasks_head_scope_is_source_or_responsible_vertical(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-10/FR-TSK-16: a Head owns their verticals, not other verticals."""
    own = _make_task(conn, creator_id=AH, source_vertical_id=1, responsible_vertical_id=1)
    responsible = _make_task(
        conn,
        creator_id=AH_V2,
        source_vertical_id=2,
        responsible_vertical_id=1,
    )
    foreign = _make_task(
        conn,
        creator_id=AH_V2,
        source_vertical_id=2,
        responsible_vertical_id=2,
    )

    assert _task_ids(visible_tasks(conn, viewer_person_id=HEAD)) == [own, responsible]
    # The cross-vertical task's source vertical is Transport, so its Head sees
    # it too; the PR-only task stays out of that Head's scope.
    assert _task_ids(visible_tasks(conn, viewer_person_id=HEAD_V2)) == [
        responsible,
        foreign,
    ]


def test_visible_tasks_ah_scope_is_only_tasks_they_created(
    conn: sqlite3.Connection,
) -> None:
    """Matrix row 10 / FR-TSK-16: an AH sees the tasks they created."""
    created = _make_task(
        conn,
        creator_id=AH,
        source_vertical_id=1,
        responsible_vertical_id=2,
    )
    _make_task(conn, creator_id=OTHER_AH, source_vertical_id=1, responsible_vertical_id=1)
    created_again = _make_task(
        conn, creator_id=AH, source_vertical_id=1, responsible_vertical_id=1
    )

    assert _task_ids(visible_tasks(conn, viewer_person_id=AH)) == [
        created,
        created_again,
    ]


def test_visible_tasks_volunteer_scope_is_assignments_plus_open_pool(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-04/FR-UI-02: own active assignments plus live OPEN tasks in the vertical."""
    assigned = _make_task(conn, source_vertical_id=1, responsible_vertical_id=1)
    _assign(conn, assigned, VOLUNTEER)
    open_own = _make_task(
        conn,
        assignment_mode="OPEN",
        people_needed=3,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )
    _make_task(
        conn,
        assignment_mode="OPEN",
        people_needed=2,
        source_vertical_id=2,
        responsible_vertical_id=2,
    )
    other_assignee = _make_task(conn, source_vertical_id=1, responsible_vertical_id=1)
    _assign(conn, other_assignee, VOLUNTEER2)
    denied_row = _make_task(conn, source_vertical_id=1, responsible_vertical_id=1)
    _assign(conn, denied_row, VOLUNTEER, agreement="DENIED")
    cancelled_open = _make_task(
        conn,
        assignment_mode="OPEN",
        people_needed=2,
        source_vertical_id=1,
        responsible_vertical_id=1,
        cancellation="CANCELLED",
    )

    assert _task_ids(visible_tasks(conn, viewer_person_id=VOLUNTEER)) == [
        assigned,
        open_own,
    ]
    assert cancelled_open not in _task_ids(
        visible_tasks(conn, viewer_person_id=VOLUNTEER)
    )
    assert _task_ids(
        visible_tasks(conn, viewer_person_id=VOLUNTEER, include_cancelled=True)
    )[-1] == cancelled_open


def test_visible_tasks_summary_carries_the_read_fields(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.5: TaskSummary exposes the task's stable read fields and roll-up."""
    task_id = _make_task(
        conn,
        assignment_mode="OPEN",
        people_needed=2,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )
    _assign(conn, task_id, VOLUNTEER, agreement="ACCEPTED", work_status="COMPLETED")
    _assign(conn, task_id, VOLUNTEER2, agreement="ACCEPTED")

    (summary,) = visible_tasks(conn, viewer_person_id=ADMIN)
    assert summary.id == task_id
    assert summary.task_type == "INTERNAL"
    assert summary.source_vertical_id == 1
    assert summary.responsible_vertical_id == 1
    assert summary.creator_id == AH
    assert summary.description == "Arrange the generator"
    assert summary.assignment_mode == "OPEN"
    assert summary.people_needed == 2
    assert summary.people_needed_unlimited is False
    assert summary.cancellation == "ACTIVE"
    assert summary.version == 1
    assert summary.accepted_count == 2
    assert summary.completed_count == 1


# =========================================================================
# W5 — progress roll-up and its permission row
# =========================================================================


def test_task_progress_rolls_up_accepted_and_completed(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-07/AC-16: progress reports each assignee and the roll-up counts."""
    task_id = _make_task(
        conn,
        assignment_mode="OPEN",
        people_needed=3,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )
    _assign(conn, task_id, VOLUNTEER, agreement="ACCEPTED", work_status="COMPLETED")
    _assign(conn, task_id, VOLUNTEER2, agreement="ACCEPTED", work_status="IN_PROGRESS")
    _assign(conn, task_id, VOL_V2, agreement="DENIED")

    progress = task_progress(conn, task_id, viewer_person_id=ADMIN)

    assert isinstance(progress, TaskProgress)
    assert progress.task_id == task_id
    assert progress.people_needed == 3
    assert progress.accepted_count == 2
    assert progress.completed_count == 1
    assert progress.assignees == (
        TaskAssigneeProgress(
            person_id=VOLUNTEER,
            name="Neha",
            agreement="ACCEPTED",
            work_status="COMPLETED",
            cancelled=False,
        ),
        TaskAssigneeProgress(
            person_id=VOLUNTEER2,
            name="Isha",
            agreement="ACCEPTED",
            work_status="IN_PROGRESS",
            cancelled=False,
        ),
        TaskAssigneeProgress(
            person_id=VOL_V2,
            name="Ravi",
            agreement="DENIED",
            work_status="NOT_STARTED",
            cancelled=False,
        ),
    )


def test_task_progress_head_is_limited_to_own_vertical(
    conn: sqlite3.Connection,
) -> None:
    """Matrix row 10: a Head reads progress for their own vertical only."""
    own = _make_task(conn, source_vertical_id=1, responsible_vertical_id=1)
    foreign = _make_task(
        conn, creator_id=AH_V2, source_vertical_id=2, responsible_vertical_id=2
    )

    assert task_progress(conn, own, viewer_person_id=HEAD).task_id == own
    with pytest.raises(TaskAuthorizationError) as excinfo:
        task_progress(conn, foreign, viewer_person_id=HEAD)

    assert excinfo.value.reason == REASON_NOT_TASK_VIEWER
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == HEAD
    assert int(denial["subject_id"]) == foreign


def test_task_progress_ah_is_limited_to_created_tasks(
    conn: sqlite3.Connection,
) -> None:
    """Matrix row 10: an AH reads progress for tasks they created."""
    created = _make_task(conn, creator_id=AH, source_vertical_id=1)
    foreign = _make_task(conn, creator_id=OTHER_AH, source_vertical_id=1)

    assert task_progress(conn, created, viewer_person_id=AH).task_id == created
    with pytest.raises(TaskAuthorizationError):
        task_progress(conn, foreign, viewer_person_id=AH)


def test_task_progress_volunteer_is_limited_to_own_assignments(
    conn: sqlite3.Connection,
) -> None:
    """Matrix row 10: a Volunteer reads progress for tasks assigned to them."""
    assigned = _make_task(conn, source_vertical_id=1)
    _assign(conn, assigned, VOLUNTEER, agreement="ACCEPTED")
    pool = _make_task(
        conn,
        assignment_mode="OPEN",
        people_needed=2,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )

    assert task_progress(conn, assigned, viewer_person_id=VOLUNTEER).task_id == assigned
    # A visible OPEN pool task is not "their" task yet (no acceptance).
    with pytest.raises(TaskAuthorizationError):
        task_progress(conn, pool, viewer_person_id=VOLUNTEER)


def test_task_progress_treats_hidden_task_as_not_found(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-11/Plan §4.5: a hidden id behaves as not found for every reader."""
    pending = _make_task(conn, approval_state="PENDING")
    cancelled = _make_task(conn, cancellation="CANCELLED")

    with pytest.raises(TaskNotFoundError):
        task_progress(conn, pending, viewer_person_id=ADMIN)
    with pytest.raises(TaskNotFoundError):
        task_progress(conn, cancelled, viewer_person_id=ADMIN)
    assert _denials(conn) == []

# =========================================================================
# W6 — claim
# =========================================================================


def test_claim_open_task_accepts_and_records(conn: sqlite3.Connection) -> None:
    """FR-TSK-04/AC-08: a claim is an explicit commitment with evidence."""
    task_id = _make_task(conn, assignment_mode="OPEN", people_needed=2)

    claim_open_task(conn, task_id, VOLUNTEER)

    (row,) = _assignee_rows(conn, task_id)
    assert str(row["agreement"]) == "ACCEPTED"
    assert str(row["agreed_at"]).strip() != ""
    assert str(row["work_status"]) == "NOT_STARTED"
    assert int(row["cancelled"]) == 0

    (history,) = _history(conn, task_id)
    assert str(history["field_name"]) == "agreement"
    assert history["old_value"] is None
    assert str(history["new_value"]) == "ACCEPTED"
    assert str(history["reason"]) == "CLAIM"
    assert int(history["actor_person_id"]) == VOLUNTEER
    assert str(history["actor_role"]) == "Volunteer"
    assert int(history["actor_vertical_id"]) == 1

    (note,) = _notifications(conn)
    assert str(note["event_type"]) == "ASSIGNEE_ACCEPTED"
    assert int(note["recipient_person_id"]) == AH
    assert int(note["task_id"]) == task_id

    summary = _summary_of(conn, task_id)
    assert summary.version == 1
    assert summary.accepted_count == 1


def test_claim_rejects_the_sixth_claim(conn: sqlite3.Connection) -> None:
    """FR-TSK-04: capacity is enforced from the accepted count (AC-08)."""
    task_id = _make_task(conn, assignment_mode="OPEN", people_needed=5)
    for person_id in (VOLUNTEER, VOLUNTEER2, VOL_A, VOL_B, VOL_C):
        claim_open_task(conn, task_id, person_id)

    with pytest.raises(TaskCapacityError) as excinfo:
        claim_open_task(conn, task_id, VOL_D)

    assert "full" in str(excinfo.value)
    accepted = [
        row
        for row in _assignee_rows(conn, task_id)
        if str(row["agreement"]) == "ACCEPTED"
    ]
    assert len(accepted) == 5


def test_two_threads_claiming_the_last_place_produce_one_winner(
    file_db: tuple[sqlite3.Connection, Path],
) -> None:
    """NFR-CON-02/AC-08: BEGIN IMMEDIATE serializes the last place."""
    connection, path = file_db
    task_id = _make_task(connection, assignment_mode="OPEN", people_needed=1)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(person_id: int) -> None:
        worker_connection = connect(path)
        try:
            barrier.wait(timeout=10)
            try:
                claim_open_task(worker_connection, task_id, person_id)
                outcomes.append("ACCEPTED")
            except TaskCapacityError:
                outcomes.append("FULL")
        finally:
            worker_connection.close()

    threads = [
        threading.Thread(target=worker, args=(person_id,))
        for person_id in (VOLUNTEER, VOLUNTEER2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["ACCEPTED", "FULL"]
    accepted = [
        row
        for row in _assignee_rows(connection, task_id)
        if str(row["agreement"]) == "ACCEPTED" and int(row["cancelled"]) == 0
    ]
    assert len(accepted) == 1


def test_claim_rejects_non_volunteers_and_other_verticals(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-04/FR-AUT-07: only an active Volunteer of the responsible vertical."""
    task_id = _make_task(conn, assignment_mode="OPEN", people_needed=2)

    with pytest.raises(TaskAuthorizationError) as excinfo:
        claim_open_task(conn, task_id, AH)
    assert excinfo.value.reason == "NOT_VOLUNTEER"
    with pytest.raises(TaskAuthorizationError) as excinfo:
        claim_open_task(conn, task_id, VOL_V2)
    assert excinfo.value.reason == "NOT_IN_VERTICAL"
    with pytest.raises(TaskAuthorizationError) as excinfo:
        claim_open_task(conn, task_id, INACTIVE)
    assert excinfo.value.reason == "ACTOR_INACTIVE"

    assert len(_denials(conn)) == 3
    assert _assignee_rows(conn, task_id) == []


def test_claim_requires_an_open_task(conn: sqlite3.Connection) -> None:
    """FR-TSK-04: EXPLICIT tasks are joined only by reassignment, not claims."""
    task_id = _make_task(conn)

    with pytest.raises(TaskStateError) as excinfo:
        claim_open_task(conn, task_id, VOLUNTEER)

    assert excinfo.value.code == "NOT_OPEN"


# =========================================================================
# W6 — agreement
# =========================================================================


def test_set_agreement_records_a_self_acceptance(conn: sqlite3.Connection) -> None:
    """FR-TSK-03/BR-03: agreement changes only by an explicit action."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")

    (row,) = _assignee_rows(conn, task_id)
    assert str(row["agreement"]) == "ACCEPTED"
    assert str(row["agreed_at"]).strip() != ""

    (history,) = _history(conn, task_id)
    assert str(history["field_name"]) == "agreement"
    assert str(history["old_value"]) == "AWAITING"
    assert str(history["new_value"]) == "ACCEPTED"
    assert str(history["reason"]) == "SELF_ACTION"
    assert int(history["actor_person_id"]) == VOLUNTEER

    (note,) = _notifications(conn)
    assert str(note["event_type"]) == "ASSIGNEE_ACCEPTED"
    assert int(note["recipient_person_id"]) == AH


def test_agreement_denial_is_terminal_and_frees_the_row(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-05/BR-09: denial is terminal and PB-3 allows a later re-add."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "DENIED")
    with pytest.raises(TaskStateError) as excinfo:
        set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")
    assert excinfo.value.code == "ALREADY_DENIED"

    (note,) = _notifications(conn)
    assert str(note["event_type"]) == "ASSIGNEE_DENIED"

    version = reassign_task(
        conn, task_id, AH, 1, add=(VOLUNTEER,), remove=()
    )
    assert version == 2
    rows = _assignee_rows(conn, task_id)
    assert [str(row["agreement"]) for row in rows] == ["DENIED", "AWAITING"]
    assert all(int(row["cancelled"]) == 0 for row in rows)


def test_same_agreement_value_is_rejected(conn: sqlite3.Connection) -> None:
    """FR-TSK-13: same-value transitions change nothing and are errors."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)
    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")

    with pytest.raises(TaskStateError) as excinfo:
        set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")

    assert excinfo.value.code == "NO_CHANGE"


def test_agreement_rejects_an_unknown_value_and_a_missing_row(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-13: the agreement vocabulary is exactly the SRS tokens."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    with pytest.raises(InvalidAgreementError):
        set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "MAYBE")
    with pytest.raises(TaskStateError) as excinfo:
        set_agreement(conn, task_id, VOLUNTEER2, VOLUNTEER2, "ACCEPTED")

    assert excinfo.value.code == "NOT_ASSIGNED"


def test_admin_may_accept_on_behalf_with_history_naming_the_admin(
    conn: sqlite3.Connection,
) -> None:
    """O7-3/FR-TSK-05: Admin only, actor is the Admin, on-behalf is recorded."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    set_agreement(conn, task_id, ADMIN, VOLUNTEER, "ACCEPTED")

    (history,) = _history(conn, task_id)
    assert int(history["actor_person_id"]) == ADMIN
    assert str(history["actor_role"]) == "Admin"
    assert str(history["reason"]) == "ADMIN_ON_BEHALF"


def test_non_admin_may_not_act_for_another_assignee(
    conn: sqlite3.Connection,
) -> None:
    """O7-3: nobody except Admin may accept or deny on an assignee's behalf."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    with pytest.raises(TaskAuthorizationError) as excinfo:
        set_agreement(conn, task_id, VOLUNTEER2, VOLUNTEER, "ACCEPTED")

    assert excinfo.value.reason == REASON_NOT_ASSIGNEE
    (row,) = _assignee_rows(conn, task_id)
    assert str(row["agreement"]) == "AWAITING"
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == VOLUNTEER2
    assert int(denial["subject_id"]) == task_id


# =========================================================================
# W6 — work status
# =========================================================================


def test_work_status_transitions_and_completion_notice(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-06/FR-TSK-07/AC-16: ordered transitions and a completion notice."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)
    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")

    update_work_status(conn, task_id, VOLUNTEER, "IN_PROGRESS")
    (row,) = _assignee_rows(conn, task_id)
    assert str(row["work_status"]) == "IN_PROGRESS"

    update_work_status(conn, task_id, VOLUNTEER, "COMPLETED")
    (row,) = _assignee_rows(conn, task_id)
    assert str(row["work_status"]) == "COMPLETED"

    transitions = [
        history
        for history in _history(conn, task_id)
        if str(history["field_name"]) == "work_status"
    ]
    assert [(str(h["old_value"]), str(h["new_value"])) for h in transitions] == [
        ("NOT_STARTED", "IN_PROGRESS"),
        ("IN_PROGRESS", "COMPLETED"),
    ]
    assert all(str(h["reason"]) == "STATUS_UPDATE" for h in transitions)

    completion = [
        note
        for note in _notifications(conn)
        if str(note["event_type"]) == "WORK_COMPLETED"
    ]
    (note,) = completion
    assert int(note["recipient_person_id"]) == AH

    with pytest.raises(TaskStateError) as excinfo:
        update_work_status(conn, task_id, VOLUNTEER, "IN_PROGRESS")
    assert excinfo.value.code == "COMPLETED_FINAL"


def test_work_status_rejects_no_change_and_skipped_steps(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-13: same value, skipped steps and an unaccepted row are errors."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    with pytest.raises(TaskStateError) as excinfo:
        update_work_status(conn, task_id, VOLUNTEER, "IN_PROGRESS")
    assert excinfo.value.code == "NOT_ACCEPTED"

    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")
    with pytest.raises(TaskStateError) as excinfo:
        update_work_status(conn, task_id, VOLUNTEER, "COMPLETED")
    assert excinfo.value.code == "INVALID_TRANSITION"

    update_work_status(conn, task_id, VOLUNTEER, "IN_PROGRESS")
    with pytest.raises(TaskStateError) as excinfo:
        update_work_status(conn, task_id, VOLUNTEER, "IN_PROGRESS")
    assert excinfo.value.code == "NO_CHANGE"


def test_admin_may_override_work_status(conn: sqlite3.Connection) -> None:
    """O7-5/FR-TSK-06: Admin override is recorded with the Admin as actor."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)
    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")

    update_work_status(
        conn, task_id, ADMIN, "IN_PROGRESS", assignee_person_id=VOLUNTEER
    )

    (history,) = [
        row
        for row in _history(conn, task_id)
        if str(row["field_name"]) == "work_status"
    ]
    assert int(history["actor_person_id"]) == ADMIN
    assert str(history["reason"]) == "ADMIN_OVERRIDE"


def test_another_person_may_not_update_work_status(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-06: only the assignee or an Admin may change work status."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)
    set_agreement(conn, task_id, VOLUNTEER, VOLUNTEER, "ACCEPTED")

    with pytest.raises(TaskAuthorizationError) as excinfo:
        update_work_status(
            conn, task_id, VOLUNTEER2, "IN_PROGRESS", assignee_person_id=VOLUNTEER
        )

    assert excinfo.value.reason == REASON_NOT_ASSIGNEE
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == VOLUNTEER2


# =========================================================================
# W6 — edit
# =========================================================================


def test_edit_task_records_one_history_row_per_changed_field(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-11/FR-TSK-16: each changed field gets its own history row."""
    task_id = _make_task(
        conn,
        description="Old description",
        assignment_mode="OPEN",
        people_needed=3,
    )
    _assign(conn, task_id, VOLUNTEER, agreement="ACCEPTED")

    version = edit_task(
        conn,
        task_id,
        AH,
        1,
        TaskEdit(
            description="  New description  ",
            people_needed=4,
            attributes=(("venue", "Hall A"),),
        ),
    )

    assert version == 2
    summary = _summary_of(conn, task_id)
    assert summary.description == "New description"
    assert summary.people_needed == 4
    assert summary.version == 2

    attributes = conn.execute(
        "SELECT attr_key, attr_value FROM task_attribute WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    assert [(str(row["attr_key"]), str(row["attr_value"])) for row in attributes] == [
        ("venue", "Hall A")
    ]

    changed = {
        str(row["field_name"]): (row["old_value"], row["new_value"])
        for row in _history(conn, task_id)
    }
    assert changed == {
        "description": ("Old description", "New description"),
        "people_needed": ("3", "4"),
        "attribute:venue": (None, "Hall A"),
    }
    for row in _history(conn, task_id):
        assert int(row["actor_person_id"]) == AH
        assert str(row["actor_role"]) == "AH"
        assert int(row["actor_vertical_id"]) == 1
        assert str(row["reason"]) == "EDIT"

    (note,) = _notifications(conn)
    assert str(note["event_type"]) == "TASK_EDITED"
    assert int(note["recipient_person_id"]) == VOLUNTEER


def test_edit_replaces_the_attribute_set(conn: sqlite3.Connection) -> None:
    """FR-TSK-02: attributes are a replaceable set with unique keys."""
    task_id = _make_task(conn)
    edit_task(
        conn, task_id, AH, 1, TaskEdit(attributes=(("venue", "Hall A"), ("time", "5pm")))
    )

    version = edit_task(
        conn, task_id, AH, 2, TaskEdit(attributes=(("venue", "Hall B"),))
    )

    assert version == 3
    rows = conn.execute(
        "SELECT attr_key, attr_value FROM task_attribute WHERE task_id = ? ORDER BY attr_key",
        (task_id,),
    ).fetchall()
    assert [(str(row["attr_key"]), str(row["attr_value"])) for row in rows] == [
        ("venue", "Hall B")
    ]
    changed = {
        str(row["field_name"]): (row["old_value"], row["new_value"])
        for row in _history(conn, task_id)
        if str(row["field_name"]).startswith("attribute:")
    }
    assert changed == {
        "attribute:time": ("5pm", None),
        "attribute:venue": ("Hall A", "Hall B"),
    }


def test_edit_is_limited_to_creator_head_of_responsible_and_admin(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-10/FR-TSK-16: authority, never authorship."""
    task_id = _make_task(conn)

    assert edit_task(conn, task_id, HEAD, 1, TaskEdit(description="By head")) == 2
    assert edit_task(conn, task_id, ADMIN, 2, TaskEdit(description="By admin")) == 3

    with pytest.raises(TaskAuthorizationError) as excinfo:
        edit_task(conn, task_id, OTHER_AH, 3, TaskEdit(description="By other AH"))
    assert excinfo.value.reason == "NOT_TASK_OWNER"
    with pytest.raises(TaskAuthorizationError) as excinfo:
        edit_task(conn, task_id, VOLUNTEER, 3, TaskEdit(description="By volunteer"))
    assert excinfo.value.reason == "NOT_TASK_OWNER"

    denials = _denials(conn)
    assert [int(row["actor_person_id"]) for row in denials] == [OTHER_AH, VOLUNTEER]
    assert _summary_of(conn, task_id).version == 3


def test_edit_rejects_an_empty_or_unchanged_change(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-16: an edit must change something."""
    task_id = _make_task(conn, description="Stay")

    with pytest.raises(TaskStateError) as excinfo:
        edit_task(conn, task_id, AH, 1, TaskEdit())
    assert excinfo.value.code == "NO_CHANGE"
    with pytest.raises(TaskStateError) as excinfo:
        edit_task(conn, task_id, AH, 1, TaskEdit(description="  Stay  "))
    assert excinfo.value.code == "NO_CHANGE"

    assert _history(conn, task_id) == []
    assert _summary_of(conn, task_id).version == 1


def test_edit_rejects_invalid_field_values(conn: sqlite3.Connection) -> None:
    """FR-TSK-02: OPEN-only capacity and unique attribute keys."""
    explicit = _make_task(conn)
    with pytest.raises(TaskStateError) as excinfo:
        edit_task(conn, explicit, AH, 1, TaskEdit(people_needed=2))
    assert excinfo.value.code == "NOT_OPEN"

    open_task = _make_task(
        conn, assignment_mode="OPEN", people_needed=2
    )
    _assign(conn, open_task, VOLUNTEER, agreement="ACCEPTED")
    _assign(conn, open_task, VOLUNTEER2, agreement="ACCEPTED")
    with pytest.raises(TaskStateError) as excinfo:
        edit_task(conn, open_task, AH, 1, TaskEdit(people_needed=1))
    assert excinfo.value.code == "CAPACITY_BELOW_ACCEPTED"

    with pytest.raises(TaskStateError) as excinfo:
        edit_task(
            conn,
            open_task,
            AH,
            1,
            TaskEdit(attributes=(("venue", "A"), ("venue", "B"))),
        )
    assert excinfo.value.code == "DUPLICATE_ATTRIBUTE"


def test_stale_edit_returns_current_values_and_records_both_attempts(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-12/AC-19: stale edit returns the snapshot and is recorded."""
    task_id = _make_task(conn, description="First")
    assert edit_task(conn, task_id, AH, 1, TaskEdit(description="Second")) == 2

    with pytest.raises(StaleTaskVersionError) as excinfo:
        edit_task(conn, task_id, AH, 1, TaskEdit(description="Third"))

    assert excinfo.value.expected == 1
    assert excinfo.value.current == 2
    assert excinfo.value.snapshot.version == 2
    assert excinfo.value.snapshot.description == "Second"
    assert _summary_of(conn, task_id).description == "Second"

    stale_rows = [
        row
        for row in _history(conn, task_id)
        if str(row["reason"]) == "STALE_EDIT"
    ]
    (stale,) = stale_rows
    assert str(stale["field_name"]) == "version"
    assert str(stale["old_value"]) == "1"
    assert str(stale["new_value"]) == "2"

    assert edit_task(conn, task_id, AH, 2, TaskEdit(description="Third")) == 3
    edit_rows = [
        row
        for row in _history(conn, task_id)
        if str(row["reason"]) == "EDIT"
    ]
    assert len(edit_rows) == 2


# =========================================================================
# W6 — reassign
# =========================================================================


def test_reassign_adds_and_removes_with_history_and_notices(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-10/FR-TSK-16: reassignment is versioned, recorded and notified."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    version = reassign_task(
        conn, task_id, AH, 1, add=(VOLUNTEER2,), remove=(VOLUNTEER,)
    )

    assert version == 2
    rows = _assignee_rows(conn, task_id)
    assert [(int(r["person_id"]), int(r["cancelled"])) for r in rows] == [
        (VOLUNTEER, 1),
        (VOLUNTEER2, 0),
    ]
    assert str(rows[1]["agreement"]) == "AWAITING"

    history = [
        row for row in _history(conn, task_id) if str(row["field_name"]) == "assignee"
    ]
    assert len(history) == 2
    assert all(str(row["reason"]) == "REASSIGN" for row in history)
    assert all(int(row["actor_person_id"]) == AH for row in history)

    notices = [
        note
        for note in _notifications(conn)
        if str(note["event_type"]) == "TASK_REASSIGNED"
    ]
    assert sorted(int(note["recipient_person_id"]) for note in notices) == [
        VOLUNTEER,
        VOLUNTEER2,
    ]


def test_reassign_authority_and_staleness(conn: sqlite3.Connection) -> None:
    """FR-TSK-10/FR-TSK-12: non-owners are denied; stale versions are recorded."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    with pytest.raises(TaskAuthorizationError):
        reassign_task(conn, task_id, OTHER_AH, 1, add=(VOLUNTEER2,), remove=())
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == OTHER_AH

    assert reassign_task(conn, task_id, AH, 1, add=(VOLUNTEER2,), remove=()) == 2
    with pytest.raises(StaleTaskVersionError) as excinfo:
        reassign_task(conn, task_id, AH, 1, add=(VOL_A,), remove=())
    assert excinfo.value.current == 2
    assert any(
        str(row["reason"]) == "STALE_EDIT" for row in _history(conn, task_id)
    )


def test_reassign_rejects_inactive_and_unassigned_targets(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-07/PB-3: no new row for a deactivated person; removals must exist."""
    task_id = _make_task(conn)
    _assign(conn, task_id, VOLUNTEER)

    with pytest.raises(TaskStateError) as excinfo:
        reassign_task(conn, task_id, AH, 1, add=(INACTIVE,), remove=())
    assert excinfo.value.code == "ASSIGNEE_INACTIVE"

    with pytest.raises(TaskStateError) as excinfo:
        reassign_task(conn, task_id, AH, 1, add=(), remove=(VOL_V2,))
    assert excinfo.value.code == "NOT_ASSIGNED"

    with pytest.raises(TaskStateError) as excinfo:
        reassign_task(conn, task_id, AH, 1, add=(), remove=())
    assert excinfo.value.code == "NO_CHANGE"


# =========================================================================
# W6 — cancel
# =========================================================================


def test_cancel_marks_task_and_unfinished_assignees(
    conn: sqlite3.Connection,
) -> None:
    """FR-TSK-08: cancel closes the task and every unfinished assignment."""
    task_id = _make_task(conn)
    _assign(
        conn,
        task_id,
        VOLUNTEER,
        agreement="ACCEPTED",
        work_status="COMPLETED",
    )
    _assign(
        conn,
        task_id,
        VOLUNTEER2,
        agreement="ACCEPTED",
        work_status="IN_PROGRESS",
    )

    cancel_task(conn, task_id, AH, "venue closed")

    summary = _summary_of(conn, task_id, include_cancelled=True)
    assert summary.cancellation == "CANCELLED"
    assert summary.version == 2
    rows = _assignee_rows(conn, task_id)
    assert [(int(r["person_id"]), int(r["cancelled"])) for r in rows] == [
        (VOLUNTEER, 0),
        (VOLUNTEER2, 1),
    ]

    (history,) = _history(conn, task_id)
    assert str(history["field_name"]) == "cancellation"
    assert str(history["old_value"]) == "ACTIVE"
    assert str(history["new_value"]) == "CANCELLED"
    assert str(history["reason"]) == "venue closed"
    assert int(history["actor_person_id"]) == AH

    notices = [
        note
        for note in _notifications(conn)
        if str(note["event_type"]) == "TASK_CANCELLED"
    ]
    (notice,) = notices
    assert int(notice["recipient_person_id"]) == VOLUNTEER2

    with pytest.raises(TaskNotFoundError):
        cancel_task(conn, task_id, AH, "again")
    with pytest.raises(TaskNotFoundError):
        claim_open_task(conn, task_id, VOL_E)


def test_cancel_authority_is_creator_head_of_responsible_vertical_or_admin(
    conn: sqlite3.Connection,
) -> None:
    """O7-4/FR-TSK-08/FR-TSK-16: the approved cancel authority."""
    task_id = _make_task(
        conn, source_vertical_id=1, responsible_vertical_id=2
    )

    with pytest.raises(TaskAuthorizationError) as excinfo:
        cancel_task(conn, task_id, HEAD, "wrong vertical")
    assert excinfo.value.reason == "NOT_TASK_OWNER"
    with pytest.raises(TaskAuthorizationError):
        cancel_task(conn, task_id, VOLUNTEER, "not mine")

    cancel_task(conn, task_id, HEAD_V2, "transport closed")

    (history,) = _history(conn, task_id)
    assert int(history["actor_person_id"]) == HEAD_V2
    assert str(history["actor_role"]) == "Head"
    assert int(history["actor_vertical_id"]) == 2


def test_cancel_requires_a_reason(conn: sqlite3.Connection) -> None:
    """FR-TSK-08: the cancellation is recorded with its reason."""
    task_id = _make_task(conn)

    with pytest.raises(TaskStateError) as excinfo:
        cancel_task(conn, task_id, AH, "   ")

    assert excinfo.value.code == "REASON_REQUIRED"


def test_admin_may_cancel_any_live_task(conn: sqlite3.Connection) -> None:
    """FR-TSK-16/FR-AUT-04: Admin authority covers cancellation."""
    task_id = _make_task(conn)

    cancel_task(conn, task_id, ADMIN, "admin decision")

    assert _summary_of(conn, task_id, include_cancelled=True).cancellation == (
        "CANCELLED"
    )


# =========================================================================
# W6 — hidden ids and concurrency
# =========================================================================


def test_hidden_task_ids_behave_as_not_found_for_every_mutator(
    conn: sqlite3.Connection,
) -> None:
    """Plan Â§4.5: a hidden id is never acted on by any write path."""
    pending = _make_task(conn, approval_state="PENDING")
    _assign(conn, pending, VOLUNTEER)

    calls = [
        lambda: claim_open_task(conn, pending, VOLUNTEER),
        lambda: set_agreement(conn, pending, VOLUNTEER, VOLUNTEER, "ACCEPTED"),
        lambda: update_work_status(conn, pending, VOLUNTEER, "IN_PROGRESS"),
        lambda: edit_task(conn, pending, AH, 1, TaskEdit(description="x")),
        lambda: reassign_task(conn, pending, AH, 1, add=(VOLUNTEER2,), remove=()),
        lambda: cancel_task(conn, pending, AH, "hidden"),
    ]
    for call in calls:
        with pytest.raises(TaskNotFoundError):
            call()

    (row,) = _assignee_rows(conn, pending)
    assert str(row["agreement"]) == "AWAITING"
    assert _history(conn, pending) == []
    assert _notifications(conn) == []
    assert _denials(conn) == []


def test_two_concurrent_edits_serialize_on_version(
    file_db: tuple[sqlite3.Connection, Path],
) -> None:
    """NFR-CON-02/FR-TSK-12: one edit wins; the loser gets a stale error."""
    connection, path = file_db
    task_id = _make_task(connection, description="start")
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker(description: str) -> None:
        worker_connection = connect(path)
        try:
            barrier.wait(timeout=10)
            try:
                outcomes.append(
                    edit_task(
                        worker_connection,
                        task_id,
                        AH,
                        1,
                        TaskEdit(description=description),
                    )
                )
            except StaleTaskVersionError as error:
                outcomes.append(error)
        finally:
            worker_connection.close()

    threads = [
        threading.Thread(target=worker, args=("edit one",)),
        threading.Thread(target=worker, args=("edit two",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(str(outcome) for outcome in outcomes) == [
        "2",
        "stale task version: expected 1, current 2",
    ]
    summary = _summary_of(connection, task_id)
    assert summary.version == 2
    assert summary.description in ("edit one", "edit two")
    reasons = [str(row["reason"]) for row in _history(connection, task_id)]
    assert reasons.count("EDIT") == 1
    assert reasons.count("STALE_EDIT") == 1
