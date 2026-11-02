"""Cross-vertical approval service tests (Phase 8).

Covers SRS FR-XV-04/05/06, FR-AUT-01/05/06, AC-15, NFR-CON-02, NFR-SEC-03(c),
BR-08 and FR-NOT-01/05, with the owner decisions O8-1..O8-3 from
``docs/OWNER_ANSWERS.md``.

Test-first: this module is written before ``backend/approvals.py`` and before
the Phase 6 request-history insert. The RED evidence for each workstream is the
run recorded at its checkpoint.

Architecture pins: the backend authorizes every read and decision server-side
(FR-AUT-01); a refused attempt is recorded in ``authorization_denial``
(FR-AUT-06); a decision is one ``BEGIN IMMEDIATE`` transaction that inserts the
single decision row, flips ``task.approval_state`` without touching
``task.version`` and appends history (FR-XV-04/06, NFR-CON-02); notifications
are staged through the shared helper with the Phase 6/7 dedupe keys (FR-NOT-05).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend import approvals
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_events import EVENT_CROSS_VERTICAL_DECISION
from backend.review import accept_proposal
from backend.tasks import visible_tasks

# --- scenario constants ---------------------------------------------------

AH = 1
VOLUNTEER = 2
HEAD = 4
ADMIN = 5
AH_V2 = 6
HEAD_V2 = 7
VOL_V2 = 8
VOL_V3 = 9
INACTIVE = 10
OTHER_AH = 11
VOLUNTEER3 = 12
VOL_V2_B = 14
HEAD_V2_B = 15
AH_V2_B = 16

_SOURCE_VERTICAL = 1
_RESPONSIBLE_VERTICAL = 2


def _vertical(conn: sqlite3.Connection, vertical_id: int, name: str) -> None:
    conn.execute("INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name))


def _group(conn: sqlite3.Connection, group_id: int, vertical_id: int) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id) VALUES (?, ?, ?)",
        (group_id, f"chan-{group_id}", vertical_id),
    )


def _person(
    conn: sqlite3.Connection,
    person_id: int,
    name: str,
    role: str,
    vertical_id: int,
    *,
    active: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (person_id, name, f"+919800000{person_id:03d}", role, active, f"ROLL-{person_id:03d}"),
    )
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )


def _seed_world(conn: sqlite3.Connection) -> None:
    _vertical(conn, 1, "PR")
    _vertical(conn, 2, "Transport")
    _vertical(conn, 3, "Design")
    _group(conn, 1, 1)

    _person(conn, AH, "Aarav", "AH", 1)
    _person(conn, VOLUNTEER, "Neha", "Volunteer", 1)
    _person(conn, HEAD, "Vikram", "Head", 1)
    _person(conn, ADMIN, "Diya", "Admin", 1)
    _person(conn, AH_V2, "Kabir", "AH", 2)
    _person(conn, HEAD_V2, "Meera", "Head", 2)
    _person(conn, VOL_V2, "Ravi", "Volunteer", 2)
    _person(conn, VOL_V3, "Tara", "Volunteer", 3)
    _person(conn, INACTIVE, "Dev", "Volunteer", 1, active=0)
    _person(conn, OTHER_AH, "Sana", "AH", 1)
    _person(conn, VOLUNTEER3, "Arjun", "Volunteer", 1)
    _person(conn, VOL_V2_B, "Manav", "Volunteer", 2)
    _person(conn, HEAD_V2_B, "Ira", "Head", 2)
    _person(conn, AH_V2_B, "Nikhil", "AH", 2)

    conn.execute(
        "INSERT INTO bucket (id, group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, 1, '2026-01-01T10:00:00+00:00',"
        " '2026-01-01T10:00:10+00:00', '{}', 'PROCESSED')"
    )
    conn.execute(
        "INSERT INTO processing_attempt (id, stage, bucket_id, started_at,"
        " finished_at, outcome, model_id, model_version, config_version)"
        " VALUES (1, 'proposal_generation', 1, '2026-01-01T10:00:00+00:00',"
        " '2026-01-01T10:00:01+00:00', 'SUCCEEDED', 'model-p', '1.0.0',"
        " 'prompt-1')"
    )
    conn.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (500, 'discord', 'context-500', 1, ?,"
        " '2026-01-01T10:00:05+00:00', '2026-01-01T10:00:05+00:00',"
        " 'context line')",
        (VOLUNTEER,),
    )
    conn.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (1, 500, 1)"
    )


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    apply_migrations(connection)
    _seed_world(connection)
    yield connection
    connection.close()


def _next_message_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM source_message"
    ).fetchone()
    return int(row["n"])


def _make_proposal(
    conn: sqlite3.Connection,
    *,
    sender_person_id: int = AH,
    task_type: str = "CROSS_VERTICAL",
    responsible_vertical_id: int | None = _RESPONSIBLE_VERTICAL,
    assignment_mode: str = "EXPLICIT",
    people_needed: int | None = None,
    people_needed_unlimited: int = 0,
    assignees: tuple[tuple[str, str, int | None, str | None], ...] | None = None,
    description: str = "Transport the speakers",
) -> int:
    message_id = _next_message_id(conn)
    conn.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (?, 'discord', ?, 1, ?, '2026-01-01T10:00:02+00:00',"
        " '2026-01-01T10:00:02+00:00', 'primary line')",
        (message_id, f"primary-{message_id}", sender_person_id),
    )
    conn.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (1, ?, 1)",
        (message_id,),
    )
    if assignees is None:
        assignees = (
            (("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),)
            if assignment_mode == "EXPLICIT"
            else ()
        )
    assignees_text = ", ".join(row[0] for row in assignees) or None
    cursor = conn.execute(
        "INSERT INTO proposal (attempt_id, bucket_id, primary_message_id,"
        " ptype, description, assignment_mode, proposed_assignees_text,"
        " people_needed, people_needed_unlimited, task_type,"
        " source_vertical_id, suggested_responsible_vertical_id,"
        " structurally_valid, generated_at, model_id, model_version,"
        " prompt_version)"
        " VALUES (1, 1, ?, 'TASK', ?, ?, ?, ?, ?, ?, 1, ?, 1,"
        " '2026-01-01T10:00:01+00:00', 'model-p', '1.0.0', 'prompt-1')",
        (
            message_id,
            description,
            assignment_mode,
            assignees_text,
            people_needed,
            people_needed_unlimited,
            task_type,
            responsible_vertical_id,
        ),
    )
    proposal_id = int(cursor.lastrowid or 0)
    for text, resolution, person_id, resolved_via in assignees:
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text,"
            " resolution, person_id, resolved_via) VALUES (?, ?, ?, ?, ?)",
            (proposal_id, text, resolution, person_id, resolved_via),
        )
    return proposal_id


def _accept_request(
    conn: sqlite3.Connection,
    *,
    sender_person_id: int = AH,
    assignment_mode: str = "EXPLICIT",
    people_needed: int | None = None,
    request_message: str | None = None,
) -> tuple[int, int]:
    proposal_id = _make_proposal(
        conn,
        sender_person_id=sender_person_id,
        assignment_mode=assignment_mode,
        people_needed=people_needed,
    )
    accept_proposal(
        conn,
        proposal_id,
        actor_person_id=sender_person_id,
        expected_revision_number=None,
        request_message=request_message,
    )
    row = conn.execute("SELECT id, task_id FROM approval_request").fetchone()
    return int(row["id"]), int(row["task_id"])


def _denials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM authorization_denial ORDER BY id").fetchall()


def _notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM notification ORDER BY id").fetchall()


# =========================================================================
# W1 — read model and read-form authorization
# =========================================================================


def test_admin_can_read_a_request_without_being_a_recipient(
    conn: sqlite3.Connection,
) -> None:
    """O8-1 / FR-AUT-04: Admin override applies to the read side too."""
    request_id, task_id = _accept_request(conn, request_message="need two hands")
    view = approvals.get_approval_request(
        conn, request_id, actor_person_id=ADMIN
    )
    assert view.request_id == request_id
    assert view.task_id == task_id
    assert view.requester_person_id == AH
    assert view.requester_name == "Aarav"
    assert view.request_message == "need two hands"
    assert view.responsible_vertical_id == _RESPONSIBLE_VERTICAL
    assert view.responsible_vertical_name == "Transport"
    assert view.source_vertical_id == _SOURCE_VERTICAL
    assert view.source_vertical_name == "PR"
    assert view.task_description == "Transport the speakers"
    assert view.task_type == "CROSS_VERTICAL"
    assert view.assignment_mode == "EXPLICIT"
    assert view.decision is None
    assert view.decision_message is None


@pytest.mark.parametrize("actor", [AH_V2, HEAD_V2], ids=["recipient-ah", "recipient-head"])
def test_recipient_can_read_a_request(
    conn: sqlite3.Connection, actor: int
) -> None:
    """FR-XV-04: any one of the snapshot recipients may read the request."""
    request_id, _ = _accept_request(conn)
    view = approvals.get_approval_request(conn, request_id, actor_person_id=actor)
    assert view.request_id == request_id
    assert _denials(conn) == []


@pytest.mark.parametrize("actor", [OTHER_AH, VOLUNTEER], ids=["ah", "volunteer"])
def test_non_recipient_read_is_denied_and_recorded(
    conn: sqlite3.Connection, actor: int
) -> None:
    """FR-AUT-01/06: a direct id is not another team's queue."""
    request_id, _ = _accept_request(conn)
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.get_approval_request(conn, request_id, actor_person_id=actor)
    (denial,) = _denials(conn)
    assert str(denial["operation"]) == "approval.read"
    assert str(denial["subject_type"]) == "approval_request"
    assert int(denial["subject_id"]) == request_id
    assert int(denial["actor_person_id"]) == actor


def test_inactive_recipient_read_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-05: current roster state decides, not the snapshot."""
    request_id, _ = _accept_request(conn)
    conn.execute("UPDATE person SET active = 0 WHERE id = ?", (AH_V2_B,))
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.get_approval_request(conn, request_id, actor_person_id=AH_V2_B)
    (denial,) = _denials(conn)
    assert str(denial["reason"]) == "ACTOR_INACTIVE"


def test_demoted_recipient_read_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """O8-2 / FR-AUT-05: the snapshot records who was asked; role is current."""
    request_id, _ = _accept_request(conn)
    conn.execute("UPDATE person SET role = 'Volunteer' WHERE id = ?", (AH_V2,))
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.get_approval_request(conn, request_id, actor_person_id=AH_V2)
    assert len(_denials(conn)) == 1


def test_relocated_head_read_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """O8-2: a Head no longer in the responsible vertical loses authority."""
    request_id, _ = _accept_request(conn)
    conn.execute(
        "DELETE FROM person_vertical WHERE person_id = ? AND vertical_id = ?",
        (HEAD_V2, _RESPONSIBLE_VERTICAL),
    )
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, 3)",
        (HEAD_V2,),
    )
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.get_approval_request(conn, request_id, actor_person_id=HEAD_V2)
    assert len(_denials(conn)) == 1


def test_list_pending_approvals_scopes_by_current_role(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-01/06, FR-UI-03: Admin sees all; recipients see only their queue."""
    request_id, _ = _accept_request(conn)

    def ids(actor: int) -> list[int]:
        return [
            view.request_id
            for view in approvals.list_pending_approvals(
                conn, actor_person_id=actor
            )
        ]

    assert ids(ADMIN) == [request_id]
    assert ids(AH_V2) == [request_id]
    assert ids(HEAD_V2) == [request_id]
    assert ids(OTHER_AH) == []
    assert ids(VOLUNTEER) == []

    conn.execute("UPDATE person SET role = 'Volunteer' WHERE id = ?", (AH_V2,))
    assert ids(AH_V2) == []

    conn.execute(
        "DELETE FROM person_vertical WHERE person_id = ? AND vertical_id = ?",
        (HEAD_V2, _RESPONSIBLE_VERTICAL),
    )
    assert ids(HEAD_V2) == []
    # Listing is a scope, not a refused attempt: nothing is recorded.
    assert _denials(conn) == []


def test_read_view_has_no_source_content_or_bucket_fields(
    conn: sqlite3.Connection,
) -> None:
    """NFR-SEC-03(c): description and requester note only, never source chat."""
    request_id, _ = _accept_request(conn, request_message="bring a van")
    view = approvals.get_approval_request(conn, request_id, actor_person_id=ADMIN)
    field_names = {field.name for field in dataclasses.fields(view)}
    assert not any(
        token in name
        for name in field_names
        for token in ("content", "bucket", "raw_metadata")
    )
    assert "primary line" not in repr(view)
    assert view.request_message == "bring a van"


def test_unknown_request_raises_not_found(conn: sqlite3.Connection) -> None:
    with pytest.raises(approvals.ApprovalNotFoundError):
        approvals.get_approval_request(conn, 9999, actor_person_id=ADMIN)


# =========================================================================
# W2 — decide_request: authorization, first wins, state flip and history
# =========================================================================


def _history(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM task_history WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()


def _decision_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM approval_decision ORDER BY id").fetchall()


@pytest.mark.parametrize(
    ("actor", "decision"),
    [(ADMIN, "APPROVED"), (AH_V2, "APPROVED"), (HEAD_V2, "DENIED")],
    ids=["admin-override", "recipient-ah", "recipient-head"],
)
def test_authorized_actor_can_decide(
    conn: sqlite3.Connection, actor: int, decision: str
) -> None:
    """O8-1/O8-2, FR-XV-04: Admin override and snapshot recipients may decide."""
    request_id, task_id = _accept_request(conn)
    result = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=actor,
        decision=decision,
        message="looks good",
    )
    assert result.changed is True
    assert result.request_id == request_id
    assert result.task_id == task_id
    assert result.decision == decision
    assert result.message == "looks good"
    assert result.decided_by == actor
    (decision_row,) = _decision_rows(conn)
    assert int(decision_row["decided_by"]) == actor
    assert str(decision_row["decision"]) == decision
    assert str(decision_row["decision_message"]) == "looks good"


@pytest.mark.parametrize(
    "actor", [OTHER_AH, VOLUNTEER], ids=["non-recipient-ah", "volunteer"]
)
def test_non_recipient_decision_is_denied_and_recorded(
    conn: sqlite3.Connection, actor: int
) -> None:
    """FR-AUT-01/06: a direct decision attempt is refused and recorded."""
    request_id, _ = _accept_request(conn)
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.decide_request(
            conn, request_id, actor_person_id=actor, decision="APPROVED"
        )
    assert _decision_rows(conn) == []
    assert conn.execute("SELECT approval_state FROM task").fetchone()[0] == "PENDING"
    (denial,) = _denials(conn)
    assert str(denial["operation"]) == "approval.decide"
    assert str(denial["subject_type"]) == "approval_request"
    assert int(denial["subject_id"]) == request_id
    assert int(denial["actor_person_id"]) == actor


def test_inactive_recipient_decision_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    request_id, _ = _accept_request(conn)
    conn.execute("UPDATE person SET active = 0 WHERE id = ?", (AH_V2_B,))
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.decide_request(
            conn, request_id, actor_person_id=AH_V2_B, decision="APPROVED"
        )
    (denial,) = _denials(conn)
    assert str(denial["reason"]) == "ACTOR_INACTIVE"
    assert _decision_rows(conn) == []


def test_demoted_recipient_decision_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """O8-2: current role governs authority."""
    request_id, _ = _accept_request(conn)
    conn.execute("UPDATE person SET role = 'Volunteer' WHERE id = ?", (AH_V2,))
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.decide_request(
            conn, request_id, actor_person_id=AH_V2, decision="APPROVED"
        )
    assert len(_denials(conn)) == 1
    assert _decision_rows(conn) == []


def test_relocated_head_decision_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """O8-2: a Head moved out of the responsible vertical loses authority."""
    request_id, _ = _accept_request(conn)
    conn.execute(
        "DELETE FROM person_vertical WHERE person_id = ? AND vertical_id = ?",
        (HEAD_V2, _RESPONSIBLE_VERTICAL),
    )
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, 3)",
        (HEAD_V2,),
    )
    with pytest.raises(approvals.ApprovalAuthorizationError):
        approvals.decide_request(
            conn, request_id, actor_person_id=HEAD_V2, decision="DENIED"
        )
    assert len(_denials(conn)) == 1
    assert _decision_rows(conn) == []


def test_first_decision_wins_and_late_attempt_changes_nothing(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-04 / O8-3: later attempts see the decision and change nothing."""
    request_id, task_id = _accept_request(conn)
    first = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=AH_V2,
        decision="APPROVED",
        message="approved with the van",
    )
    history_before = _history(conn, task_id)
    notifications_before = _notifications(conn)

    late = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=HEAD_V2,
        decision="DENIED",
        message="too late",
    )

    assert first.changed is True
    assert late.changed is False
    assert late.decision == "APPROVED"
    assert late.message == "approved with the van"
    assert late.decided_by == AH_V2
    assert late.notified == 0
    assert len(_decision_rows(conn)) == 1
    assert _history(conn, task_id) == history_before
    assert _notifications(conn) == notifications_before
    task = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    assert str(task["approval_state"]) == "APPROVED"


def test_approve_flips_state_without_bumping_version_and_goes_live(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-05 / PB-3: approval is separate from task.version."""
    request_id, task_id = _accept_request(conn)
    result = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=AH_V2,
        decision="APPROVED",
        message="go ahead",
    )
    task = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    assert str(task["approval_state"]) == "APPROVED"
    assert int(task["version"]) == 1
    assert result.changed is True

    visible = visible_tasks(conn, viewer_person_id=VOL_V2)
    assert [summary.id for summary in visible] == [task_id]


def test_deny_keeps_task_hidden_and_keeps_the_decision(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-05 / BR-08: denial keeps the task, decision and message."""
    request_id, task_id = _accept_request(conn, request_message="please help")
    approvals.decide_request(
        conn,
        request_id,
        actor_person_id=HEAD_V2,
        decision="DENIED",
        message="not this week",
    )
    task = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    assert str(task["approval_state"]) == "DENIED"
    assert str(task["cancellation"]) == "ACTIVE"
    assert visible_tasks(conn, viewer_person_id=VOL_V2) == ()
    (decision,) = _decision_rows(conn)
    assert str(decision["decision"]) == "DENIED"
    assert str(decision["decision_message"]) == "not this week"
    (request,) = conn.execute("SELECT * FROM approval_request").fetchall()
    assert str(request["request_message"]) == "please help"
    assert conn.execute("SELECT COUNT(*) FROM task_assignee").fetchone()[0] == 1


def test_decision_history_records_actor_role_and_vertical(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-06 / FR-TSK-11: history carries the actor's role and vertical."""
    request_id, task_id = _accept_request(conn)
    approvals.decide_request(
        conn,
        request_id,
        actor_person_id=AH_V2,
        decision="APPROVED",
        message="bring the van",
    )
    rows = _history(conn, task_id)
    decision_history = [row for row in rows if str(row["field_name"]) == "approval_state"]
    (history,) = decision_history
    assert history["old_value"] == "PENDING"
    assert str(history["new_value"]) == "APPROVED"
    assert int(history["actor_person_id"]) == AH_V2
    assert str(history["actor_role"]) == "AH"
    assert int(history["actor_vertical_id"]) == _RESPONSIBLE_VERTICAL
    assert str(history["reason"]) == "bring the van"


def test_admin_outside_responsible_vertical_records_null_vertical(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.3/§10.5: the truthful record has no membership there."""
    request_id, task_id = _accept_request(conn)
    approvals.decide_request(
        conn, request_id, actor_person_id=ADMIN, decision="APPROVED"
    )
    history = [
        row
        for row in _history(conn, task_id)
        if str(row["field_name"]) == "approval_state"
    ][0]
    assert str(history["actor_role"]) == "Admin"
    assert history["actor_vertical_id"] is None
    assert str(history["reason"]) == "CROSS_VERTICAL_DECISION"


@pytest.mark.parametrize(
    ("decision", "message", "error"),
    [
        ("MAYBE", None, approvals.InvalidDecisionError),
        ("APPROVED", "   ", approvals.InvalidDecisionError),
    ],
    ids=["invalid-token", "blank-message"],
)
def test_invalid_decision_input_changes_nothing(
    conn: sqlite3.Connection,
    decision: str,
    message: str | None,
    error: type[Exception],
) -> None:
    request_id, task_id = _accept_request(conn)
    with pytest.raises(error):
        approvals.decide_request(
            conn,
            request_id,
            actor_person_id=AH_V2,
            decision=decision,
            message=message,
        )
    assert _decision_rows(conn) == []
    assert not [
        row
        for row in _history(conn, task_id)
        if str(row["field_name"]) == "approval_state"
    ]
    task = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    assert str(task["approval_state"]) == "PENDING"


def test_missing_request_raises_not_found(conn: sqlite3.Connection) -> None:
    with pytest.raises(approvals.ApprovalNotFoundError):
        approvals.decide_request(
            conn, 9999, actor_person_id=ADMIN, decision="APPROVED"
        )


def test_state_contradiction_is_not_silently_resolved(
    conn: sqlite3.Connection,
) -> None:
    """An APPROVED state without a decision row never invents a decision."""
    request_id, task_id = _accept_request(conn)
    conn.execute(
        "UPDATE task SET approval_state = 'APPROVED' WHERE id = ?", (task_id,)
    )
    with pytest.raises(approvals.ApprovalError) as excinfo:
        approvals.decide_request(
            conn, request_id, actor_person_id=AH_V2, decision="APPROVED"
        )
    assert str(excinfo.value) == "STATE_CONTRADICTION"
    assert _decision_rows(conn) == []


def test_two_threads_produce_exactly_one_decision(tmp_path: Path) -> None:
    """NFR-CON-02 / AC-15: BEGIN IMMEDIATE + UNIQUE(request_id) has one winner."""
    database = tmp_path / "approval-concurrency.db"
    setup = connect(database)
    apply_migrations(setup)
    _seed_world(setup)
    request_id, task_id = _accept_request(setup)
    setup.close()

    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def decide(actor: int, decision: str) -> None:
        connection = connect(database)
        try:
            barrier.wait(timeout=5)
            result = approvals.decide_request(
                connection,
                request_id,
                actor_person_id=actor,
                decision=decision,
                message=decision.casefold(),
            )
            with lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=decide, args=(AH_V2, "APPROVED")),
        threading.Thread(target=decide, args=(HEAD_V2, "DENIED")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert sum(1 for result in results if result.changed) == 1  # type: ignore[attr-defined]

    check = connect(database)
    try:
        assert check.execute("SELECT COUNT(*) FROM approval_decision").fetchone()[0] == 1
        assert (
            check.execute(
                "SELECT COUNT(*) FROM task_history WHERE field_name = 'approval_state'"
            ).fetchone()[0]
            == 1
        )
        state = check.execute(
            "SELECT approval_state FROM task WHERE id = ?", (task_id,)
        ).fetchone()[0]
        decision = check.execute(
            "SELECT decision FROM approval_decision"
        ).fetchone()[0]
        assert state == decision
    finally:
        check.close()


# =========================================================================
# W3 — notification staging on approve/deny (FR-XV-05, FR-NOT-01/05)
# =========================================================================


def _events(conn: sqlite3.Connection, event_type: str) -> list[sqlite3.Row]:
    return [
        row
        for row in _notifications(conn)
        if str(row["event_type"]) == event_type
    ]


def test_approval_stages_requester_and_explicit_assignee_rows(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-05/FR-NOT-01: requester + every active AWAITING assignee, once."""
    request_id, task_id = _accept_request(conn)
    result = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=AH_V2,
        decision="APPROVED",
        message="bring the van",
    )

    decisions = _events(conn, EVENT_CROSS_VERTICAL_DECISION)
    (requester_row,) = decisions
    assert int(requester_row["recipient_person_id"]) == AH
    assert int(requester_row["approval_request_id"]) == request_id
    assert requester_row["task_id"] is None
    assert str(requester_row["dedupe_key"]) == (
        f"CROSS_VERTICAL_DECISION:{request_id}:{AH}"
    )
    assert str(requester_row["delivery_state"]) == "PENDING"

    assignments = _events(conn, "TASK_ASSIGNED")
    (assignee_row,) = assignments
    assert int(assignee_row["recipient_person_id"]) == VOL_V2
    assert int(assignee_row["task_id"]) == task_id
    assert assignee_row["approval_request_id"] is None
    assert str(assignee_row["dedupe_key"]) == f"TASK_ASSIGNED:{task_id}:{VOL_V2}"

    assert _events(conn, "OPEN_TASK_AVAILABLE") == []
    assert result.notified == 2


def test_approval_of_open_task_notifies_responsible_volunteers(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-05: OPEN approval goes live to the responsible vertical's pool."""
    request_id, task_id = _accept_request(
        conn, assignment_mode="OPEN", people_needed=2
    )
    result = approvals.decide_request(
        conn, request_id, actor_person_id=HEAD_V2, decision="APPROVED"
    )

    available = _events(conn, "OPEN_TASK_AVAILABLE")
    assert sorted(int(row["recipient_person_id"]) for row in available) == [
        VOL_V2,
        VOL_V2_B,
    ]
    assert all(int(row["task_id"]) == task_id for row in available)
    assert all(
        str(row["dedupe_key"]) == f"OPEN_TASK_AVAILABLE:{task_id}:{int(row['recipient_person_id'])}"
        for row in available
    )
    assert _events(conn, "TASK_ASSIGNED") == []
    assert result.notified == 3  # requester + two responsible volunteers


def test_denial_notifies_only_the_requester(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-05: a denial never tells assignees or volunteers."""
    request_id, task_id = _accept_request(conn)
    result = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=AH_V2,
        decision="DENIED",
        message="not this week",
    )
    (requester_row,) = _events(conn, EVENT_CROSS_VERTICAL_DECISION)
    assert str(requester_row["event_type"]) == EVENT_CROSS_VERTICAL_DECISION
    assert int(requester_row["recipient_person_id"]) == AH
    assert int(requester_row["approval_request_id"]) == request_id
    assert _events(conn, "TASK_ASSIGNED") == []
    assert _events(conn, "OPEN_TASK_AVAILABLE") == []
    assert result.notified == 1
    # The message travels with the decision the notification points at.
    decision = _decision_rows(conn)[0]
    assert str(decision["decision_message"]) == "not this week"


def test_late_attempt_stages_no_duplicate_notifications(
    conn: sqlite3.Connection,
) -> None:
    """FR-NOT-05: retries and late attempts stage nothing twice."""
    request_id, task_id = _accept_request(conn)
    approvals.decide_request(
        conn, request_id, actor_person_id=AH_V2, decision="APPROVED"
    )
    before = _notifications(conn)
    late = approvals.decide_request(
        conn,
        request_id,
        actor_person_id=HEAD_V2,
        decision="APPROVED",
        message="again",
    )
    assert late.changed is False
    assert late.notified == 0
    assert _notifications(conn) == before
    assert len(_events(conn, EVENT_CROSS_VERTICAL_DECISION)) == 1
    assert len(_events(conn, "TASK_ASSIGNED")) == 1
    assert _events(conn, "TASK_ASSIGNED")[0]["task_id"] == task_id


def test_inactive_assignee_is_not_notified_on_approval(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-05: only active assignees are told; inactive rows stay intact."""
    request_id, _ = _accept_request(conn)
    conn.execute("UPDATE person SET active = 0 WHERE id = ?", (VOL_V2,))
    result = approvals.decide_request(
        conn, request_id, actor_person_id=AH_V2, decision="APPROVED"
    )
    assert _events(conn, "TASK_ASSIGNED") == []
    assert result.notified == 1  # requester only
    assert conn.execute("SELECT COUNT(*) FROM task_assignee").fetchone()[0] == 1


def test_notification_inputs_use_the_phase7_helpers() -> None:
    """FR-NOT-05: the decision dedupe key is the documented Phase 8 format."""
    from backend.notification_events import cross_vertical_decision_key

    assert (
        cross_vertical_decision_key(4, 10)
        == "CROSS_VERTICAL_DECISION:4:10"
    )


# =========================================================================
# W4 — Phase 6 follow-through: request history at accept (FR-XV-06)
# =========================================================================


def test_accept_records_the_request_in_task_history(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-06: the request and its message are part of the task history."""
    request_id, task_id = _accept_request(conn, request_message="need two hands")
    rows = [
        row
        for row in _history(conn, task_id)
        if str(row["field_name"]) == "APPROVAL_REQUEST"
    ]
    (history,) = rows
    assert history["old_value"] is None
    assert str(history["new_value"]) == "PENDING"
    assert int(history["actor_person_id"]) == AH
    assert str(history["actor_role"]) == "AH"
    assert int(history["actor_vertical_id"]) == _SOURCE_VERTICAL
    assert str(history["reason"]) == "need two hands"
    # The request row itself still points at the same task and requester.
    request = conn.execute(
        "SELECT * FROM approval_request WHERE id = ?", (request_id,)
    ).fetchone()
    assert int(request["task_id"]) == task_id
    assert int(request["requested_by"]) == AH


def test_accept_without_request_message_records_a_stable_reason(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-06: a message-less request still has a non-blank history reason."""
    _, task_id = _accept_request(conn)
    rows = [
        row
        for row in _history(conn, task_id)
        if str(row["field_name"]) == "APPROVAL_REQUEST"
    ]
    (history,) = rows
    assert history["old_value"] is None
    assert str(history["new_value"]) == "PENDING"
    assert str(history["reason"]) == "CROSS_VERTICAL_REQUEST"


def test_head_cross_vertical_accept_has_no_request_history(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-02 / AC-14: a Head task has no request, so no request history."""
    proposal_id = _make_proposal(conn, sender_person_id=HEAD)
    accept_proposal(
        conn,
        proposal_id,
        actor_person_id=HEAD,
        expected_revision_number=None,
    )
    task_id = int(conn.execute("SELECT id FROM task").fetchone()["id"])
    field_names = [str(row["field_name"]) for row in _history(conn, task_id)]
    assert field_names == ["TASK_CREATED"]
    assert conn.execute("SELECT COUNT(*) FROM approval_request").fetchone()[0] == 0
