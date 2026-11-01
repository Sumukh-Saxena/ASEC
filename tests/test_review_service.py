"""Review-service tests (Phase 6, WS-2..WS-6).

Covers SRS FR-REV-01..10, FR-PRP-17, FR-AUT-06 and the owner decisions in
``docs/DECISIONS.md`` section A (OD-REV-1..7) with amendments A2..A18 from
``docs/PHASE6_PLAN.md``.

Architecture pins: the backend is the sole authority (FR-AUT-01/02); every
public operation authorizes first and records an unauthorized attempt before
raising (FR-AUT-06, A17); edits append immutable revisions and never touch the
generated proposal rows (FR-REV-03/09, A2/A10); accept/reject is one
``BEGIN IMMEDIATE`` transaction and materializes exactly one entity
(FR-REV-05, NFR-CON-02); acceptance gates run before any write (FR-REV-06,
A3/A4/A6/A13); notification rows are staged only and a hidden PENDING task
stages no assignment/availability rows (A8/A16/A18).

These tests were added after the interrupted session had already written
``backend/review.py``; RED evidence for the service itself is the mutation
battery recorded in ``docs/PHASE6_PLAN.md`` §15. Tests are never weakened to
match the implementation.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.review import (
    DEFAULT_ADMIN_ROLE_TOKENS,
    REVIEW_STATES,
    TERMINAL_REVIEW_STATES,
    AcceptanceBlockedError,
    AcceptResult,
    AssigneeSelection,
    InvalidEditError,
    NoChangesError,
    ProposalEdit,
    ProposalNotFoundError,
    ProposalReviewView,
    ReviewAuthorizationError,
    ReviewMessage,
    ReviewPolicy,
    StaleRevisionError,
    TerminalStateError,
    accept_proposal,
    get_proposal_review,
    list_reviewed_proposals_for_eval,
    load_review_policy,
    notify_proposal_ready,
    reject_proposal,
    save_proposal_edit,
    sweep_unnotified_proposals,
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
VOLUNTEER3 = 12
DUPLICATE_NAME = 13
VOL_V2_B = 14
HEAD_V2_B = 15
AH_V2_B = 16

_ASSIGNEE_DEFAULT = (("Neha", "RESOLVED", VOLUNTEER, "MENTION"),)


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
    """Verticals, roster and one processed bucket with a context message."""
    _vertical(conn, 1, "PR")
    _vertical(conn, 2, "Transport")
    _vertical(conn, 3, "Design")
    _group(conn, 1, 1)

    _person(conn, AH, "Aarav", "AH", 1)
    _person(conn, VOLUNTEER, "Neha", "Volunteer", 1)
    _person(conn, VOLUNTEER2, "Isha", "Volunteer", 1)
    _person(conn, HEAD, "Vikram", "Head", 1)
    _person(conn, ADMIN, "Diya", "Admin", 1)
    _person(conn, AH_V2, "Kabir", "AH", 2)
    _person(conn, HEAD_V2, "Meera", "Head", 2)
    _person(conn, VOL_V2, "Ravi", "Volunteer", 2)
    _person(conn, VOL_V3, "Tara", "Volunteer", 3)
    _person(conn, INACTIVE, "Dev", "Volunteer", 1, active=0)
    _person(conn, OTHER_AH, "Sana", "AH", 1)
    _person(conn, VOLUNTEER3, "Arjun", "Volunteer", 1)
    _person(conn, DUPLICATE_NAME, "Neha", "Volunteer", 1)
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
    _context_message(conn)


def _context_message(conn: sqlite3.Connection) -> int:
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
    return 500


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    apply_migrations(connection)
    _seed_world(connection)
    yield connection
    connection.close()


def _next_message_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM source_message").fetchone()
    return int(row["n"])


def _make_proposal(
    conn: sqlite3.Connection,
    *,
    sender_person_id: int | None = AH,
    ptype: str = "TASK",
    description: str | None = "Arrange the generator",
    assignment_mode: str = "EXPLICIT",
    task_type: str = "INTERNAL",
    responsible_vertical_id: int | None = None,
    people_needed: int | None = None,
    people_needed_unlimited: int = 0,
    assignees: tuple[tuple[str, str, int | None, str | None], ...] | None = None,
    attributes: tuple[tuple[str, str | None], ...] = (),
    affected_vertical_ids: tuple[int, ...] = (),
    structurally_valid: int = 1,
) -> int:
    """Insert one primary message in bucket 1 and its generated proposal."""
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
        assignees = _ASSIGNEE_DEFAULT if assignment_mode == "EXPLICIT" else ()
    assignees_text = ", ".join(row[0] for row in assignees) or None
    cursor = conn.execute(
        "INSERT INTO proposal (attempt_id, bucket_id, primary_message_id,"
        " ptype, description, assignment_mode, proposed_assignees_text,"
        " people_needed, people_needed_unlimited, task_type,"
        " source_vertical_id, suggested_responsible_vertical_id,"
        " structurally_valid, generated_at, model_id, model_version,"
        " prompt_version)"
        " VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?,"
        " '2026-01-01T10:00:01+00:00', 'model-p', '1.0.0', 'prompt-1')",
        (
            message_id,
            ptype,
            description,
            assignment_mode,
            assignees_text,
            people_needed,
            people_needed_unlimited,
            task_type,
            responsible_vertical_id,
            structurally_valid,
        ),
    )
    proposal_id = int(cursor.lastrowid or 0)
    for text, resolution, person_id, resolved_via in assignees:
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text,"
            " resolution, person_id, resolved_via) VALUES (?, ?, ?, ?, ?)",
            (proposal_id, text, resolution, person_id, resolved_via),
        )
    for key, value in attributes:
        conn.execute(
            "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
            " VALUES (?, ?, ?)",
            (proposal_id, key, value),
        )
    for vertical_id in affected_vertical_ids:
        conn.execute(
            "INSERT INTO proposal_affected_vertical (proposal_id, vertical_id)"
            " VALUES (?, ?)",
            (proposal_id, vertical_id),
        )
    return proposal_id


def _primary_message_id(conn: sqlite3.Connection, proposal_id: int) -> int:
    row = conn.execute(
        "SELECT primary_message_id FROM proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    return int(row["primary_message_id"])


def _edit(
    conn: sqlite3.Connection,
    proposal_id: int,
    changes: ProposalEdit,
    *,
    actor: int = AH,
    expected: int | None = 0,
) -> object:
    return save_proposal_edit(
        conn,
        proposal_id,
        actor_person_id=actor,
        expected_revision_number=expected,
        changes=changes,
    )


def _accept(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    actor: int = AH,
    expected: int | None = None,
    note: str | None = None,
    request_message: str | None = None,
) -> AcceptResult:
    return accept_proposal(
        conn,
        proposal_id,
        actor_person_id=actor,
        expected_revision_number=expected,
        note=note,
        request_message=request_message,
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM notification ORDER BY id").fetchall()


def _denials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM authorization_denial ORDER BY id").fetchall()


def _assert_no_entity_writes(conn: sqlite3.Connection) -> None:
    assert _count(conn, "task") == 0
    assert _count(conn, "announcement") == 0
    assert _count(conn, "proposal_decision") == 0


# =========================================================================
# WS-2 — domain model and state machine (FR-REV-01, Figure 6)
# =========================================================================


def test_review_state_vocabulary_matches_srs() -> None:
    assert REVIEW_STATES == ("PENDING", "EDITED", "ACCEPTED", "REJECTED")
    assert TERMINAL_REVIEW_STATES == ("ACCEPTED", "REJECTED")


def test_pending_to_edited_does_not_accept(conn: sqlite3.Connection) -> None:
    """FR-REV-04 / Figure 6: saving an edit never accepts."""
    proposal_id = _make_proposal(conn)
    result = _edit(conn, proposal_id, ProposalEdit(description="Edited description"))

    assert result.revision_number == 1  # type: ignore[attr-defined]
    assert result.changed_fields == ("description",)  # type: ignore[attr-defined]
    state = conn.execute("SELECT review_state FROM proposal WHERE id = ?", (proposal_id,))
    assert str(state.fetchone()["review_state"]) == "EDITED"
    _assert_no_entity_writes(conn)


def test_edited_loops_to_next_revision(conn: sqlite3.Connection) -> None:
    """Figure 6: EDITED -> EDITED loops with monotonic revision numbers."""
    proposal_id = _make_proposal(conn)
    first = _edit(conn, proposal_id, ProposalEdit(description="First"))
    second = _edit(conn, proposal_id, ProposalEdit(description="Second"), expected=1)
    third = _edit(conn, proposal_id, ProposalEdit(description="Third"), expected=2)

    assert (first.revision_number, second.revision_number, third.revision_number) == (1, 2, 3)  # type: ignore[attr-defined]
    assert [int(row["revision_number"]) for row in conn.execute(
        "SELECT revision_number FROM proposal_revision WHERE proposal_id = ?"
        " ORDER BY revision_number",
        (proposal_id,),
    )] == [1, 2, 3]


def test_terminal_states_block_edit_and_reject(conn: sqlite3.Connection) -> None:
    """Figure 6: ACCEPTED and REJECTED are strictly terminal for edit/reject."""
    accepted = _make_proposal(conn)
    _accept(conn, accepted)
    with pytest.raises(TerminalStateError):
        _edit(conn, accepted, ProposalEdit(description="Too late"))
    with pytest.raises(TerminalStateError):
        reject_proposal(conn, accepted, actor_person_id=AH)

    rejected = _make_proposal(conn)
    reject_proposal(conn, rejected, actor_person_id=AH)
    with pytest.raises(TerminalStateError):
        _edit(conn, rejected, ProposalEdit(description="Too late"))

    assert _count(conn, "proposal_revision") == 0


def test_repeat_accept_is_idempotent(conn: sqlite3.Connection) -> None:
    """FR-REV-05: a repeated accept returns the same entity and creates nothing."""
    proposal_id = _make_proposal(conn)
    first = _accept(conn, proposal_id)
    second = _accept(conn, proposal_id)

    assert first.created is True
    assert second.created is False
    assert second.entity_id == first.entity_id
    assert second.notification_count == 0
    assert _count(conn, "task") == 1
    assert _count(conn, "proposal_decision") == 1


def test_rejected_proposal_cannot_be_accepted(conn: sqlite3.Connection) -> None:
    """FR-REV-07: a rejected proposal is never reconsidered automatically."""
    proposal_id = _make_proposal(conn)
    reject_proposal(conn, proposal_id, actor_person_id=AH)

    with pytest.raises(TerminalStateError):
        _accept(conn, proposal_id)

    assert _count(conn, "task") == 0
    assert _count(conn, "announcement") == 0


# =========================================================================
# WS-3 — authorization, denial recording and presentation view
# =========================================================================


def test_reviewer_reads_presentation_view(conn: sqlite3.Connection) -> None:
    """FR-REV-02: the sender sees the draft, its source context and provenance."""
    proposal_id = _make_proposal(
        conn, attributes=(("venue", "hall A"),), affected_vertical_ids=(2,)
    )
    view = get_proposal_review(conn, proposal_id, actor_person_id=AH)

    assert isinstance(view, ProposalReviewView)
    assert view.proposal_id == proposal_id
    assert view.ptype == "TASK"
    assert view.review_state == "PENDING"
    assert view.reviewer_person_id == AH
    assert view.reviewer_name == "Aarav"
    assert view.source_vertical_id == 1
    assert view.group_id == 1
    assert view.primary_message.is_primary is True
    assert view.primary_message.message_id == _primary_message_id(conn, proposal_id)
    assert [message.message_id for message in view.context_messages] == [
        500,
        _primary_message_id(conn, proposal_id),
    ]
    provenance = {field.field: field for field in view.field_provenance}
    assert provenance["description"].original == "Arrange the generator"
    assert provenance["description"].effective == "Arrange the generator"
    assert provenance["description"].edited is False
    assert provenance["attributes"].effective == (("venue", "hall A"),)
    assert provenance["affected_vertical_ids"].effective == (2,)


def test_view_provenance_marks_edits(conn: sqlite3.Connection) -> None:
    """FR-REV-02/FR-REV-04: original and effective values stay distinguishable."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(description="Edited description"))
    view = get_proposal_review(conn, proposal_id, actor_person_id=AH)
    description = next(
        field for field in view.field_provenance if field.field == "description"
    )

    assert description.original == "Arrange the generator"
    assert description.effective == "Edited description"
    assert description.edited is True
    assert view.review_state == "EDITED"


def test_view_exposes_no_contact_fields() -> None:
    """NFR-SEC-03 / plan §10: the review view carries no contact or roster PII."""
    view_fields = {field.name for field in dataclasses.fields(ProposalReviewView)}
    message_fields = {field.name for field in dataclasses.fields(ReviewMessage)}
    forbidden = {
        "phone",
        "email",
        "roll_number",
        "discord_user_id",
        "discord_id",
        "contact",
    }
    assert not (view_fields | message_fields) & forbidden


def test_other_ah_blocked_and_denial_recorded(conn: sqlite3.Connection) -> None:
    """FR-AUT-01 / FR-AUT-06 / A17: a non-reviewer is rejected and recorded."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(ReviewAuthorizationError):
        get_proposal_review(conn, proposal_id, actor_person_id=OTHER_AH)

    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == OTHER_AH
    assert str(denial["operation"]) == "PROPOSAL_REVIEW"
    assert str(denial["subject_type"]) == "PROPOSAL"
    assert int(denial["subject_id"]) == proposal_id
    assert str(denial["reason"]) == "NOT_REVIEWER"
    assert str(denial["attempted_at"]).strip()
    _assert_no_entity_writes(conn)
    assert _count(conn, "proposal_revision") == 0
    state = conn.execute("SELECT review_state FROM proposal WHERE id = ?", (proposal_id,))
    assert str(state.fetchone()["review_state"]) == "PENDING"


def test_volunteer_blocked_and_recorded(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn)
    with pytest.raises(ReviewAuthorizationError):
        _edit(conn, proposal_id, ProposalEdit(description="Nope"), actor=VOLUNTEER)

    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == VOLUNTEER
    assert _count(conn, "proposal_revision") == 0


def test_unknown_actor_denial_has_null_actor(conn: sqlite3.Connection) -> None:
    """DR-04: a missing roster person is stored as missing, never guessed."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(ReviewAuthorizationError):
        _accept(conn, proposal_id, actor=99999)

    (denial,) = _denials(conn)
    assert denial["actor_person_id"] is None
    assert str(denial["reason"]) == "ACTOR_NOT_FOUND"
    _assert_no_entity_writes(conn)


def test_inactive_actor_blocked_and_recorded(conn: sqlite3.Connection) -> None:
    """FR-AUT-07: deactivation blocks new actions while keeping attribution."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(ReviewAuthorizationError):
        _accept(conn, proposal_id, actor=INACTIVE)

    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == INACTIVE
    assert str(denial["reason"]) == "ACTOR_INACTIVE"


def test_admin_override_allowed(conn: sqlite3.Connection) -> None:
    """FR-REV-02 / matrix: Admin may review someone else's proposal."""
    proposal_id = _make_proposal(conn)
    view = get_proposal_review(conn, proposal_id, actor_person_id=ADMIN)
    assert view.reviewer_person_id == AH
    assert _denials(conn) == []


def test_admin_token_is_trimmed_and_case_insensitive(conn: sqlite3.Connection) -> None:
    """PB-5/O7-6: Admin recognition matches policy case-insensitively with canonical role."""
    proposal_id = _make_proposal(conn)
    policy = ReviewPolicy(admin_role_tokens=("aDmIn",))
    assert (
        get_proposal_review(
            conn, proposal_id, actor_person_id=ADMIN, policy=policy
        ).proposal_id
        == proposal_id
    )

    policy = load_review_policy(_Config({"ADMIN_ROLES": " Auditor , Lead "}))
    assert policy.admin_role_tokens == ("Auditor", "Lead")
    assert load_review_policy(_Config({})).admin_role_tokens == ("Admin",)
    assert ReviewPolicy(admin_role_tokens=("admin",)).admin_role_tokens == ("admin",)
    assert DEFAULT_ADMIN_ROLE_TOKENS == ("Admin",)


class _Config:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name, default)


@pytest.mark.parametrize("operation", ["view", "edit", "accept", "reject"])
def test_denial_recorded_for_every_public_operation(
    conn: sqlite3.Connection, operation: str
) -> None:
    """A17: all four public review operations record an unauthorized attempt."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(ReviewAuthorizationError):
        if operation == "view":
            get_proposal_review(conn, proposal_id, actor_person_id=OTHER_AH)
        elif operation == "edit":
            _edit(conn, proposal_id, ProposalEdit(description="Nope"), actor=OTHER_AH)
        elif operation == "accept":
            _accept(conn, proposal_id, actor=OTHER_AH)
        else:
            reject_proposal(conn, proposal_id, actor_person_id=OTHER_AH)

    (denial,) = _denials(conn)
    assert str(denial["operation"]) == "PROPOSAL_REVIEW"
    assert int(denial["subject_id"]) == proposal_id
    assert _count(conn, "proposal_revision") == 0
    _assert_no_entity_writes(conn)


def test_repeated_denials_append(conn: sqlite3.Connection) -> None:
    """FR-AUT-06 / NFR-AUD-03: denials are append-only evidence."""
    proposal_id = _make_proposal(conn)
    for _ in range(2):
        with pytest.raises(ReviewAuthorizationError):
            get_proposal_review(conn, proposal_id, actor_person_id=OTHER_AH)
    assert _count(conn, "authorization_denial") == 2


def test_missing_proposal_reports_not_found(conn: sqlite3.Connection) -> None:
    with pytest.raises(ProposalNotFoundError):
        get_proposal_review(conn, 9999, actor_person_id=AH)


# =========================================================================
# WS-4 — edit pipeline and immutable revisions
# =========================================================================


def test_edit_creates_immutable_revision(conn: sqlite3.Connection) -> None:
    """FR-REV-03/09: who changed what, when; changed_fields are recorded."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(description="Arrange two generators"))

    (revision,) = conn.execute(
        "SELECT * FROM proposal_revision WHERE proposal_id = ?", (proposal_id,)
    ).fetchall()
    assert int(revision["revision_number"]) == 1
    assert int(revision["person_id"]) == AH
    assert json.loads(str(revision["changed_fields"])) == ["description"]
    assert str(revision["description"]) == "Arrange two generators"
    assert str(revision["changed_at"]).strip()


def test_edit_leaves_generated_proposal_row_unchanged(conn: sqlite3.Connection) -> None:
    """FR-REV-03 / AI-03 / DR-03: the generated version is never overwritten."""
    proposal_id = _make_proposal(conn)
    before = dict(conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone())
    _edit(conn, proposal_id, ProposalEdit(description="Edited"))

    after = dict(conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone())
    assert after["description"] == before["description"]
    assert after["assignment_mode"] == before["assignment_mode"]
    assert after["review_state"] == "EDITED"
    assert {key: after[key] for key in after if key != "review_state"} == {
        key: before[key] for key in before if key != "review_state"
    }


def test_manual_roster_selection_uses_manual_route(conn: sqlite3.Connection) -> None:
    """OD-REV-2/OD-REV-3: direct roster selection is stored per revision."""
    proposal_id = _make_proposal(conn)
    _edit(
        conn,
        proposal_id,
        ProposalEdit(assignees=(AssigneeSelection("Neha", person_id=VOLUNTEER),)),
    )

    (row,) = conn.execute(
        "SELECT ra.* FROM revision_assignee AS ra"
        " JOIN proposal_revision AS r ON r.id = ra.revision_id"
        " WHERE r.proposal_id = ?",
        (proposal_id,),
    ).fetchall()
    assert str(row["resolution"]) == "RESOLVED"
    assert int(row["person_id"]) == VOLUNTEER
    assert str(row["resolved_via"]) == "MANUAL"


@pytest.mark.parametrize("ptype", ["TASK", "ANNOUNCEMENT"])
def test_manual_roster_selection_of_unknown_person_is_invalid(
    conn: sqlite3.Connection, ptype: str
) -> None:
    """OD-REV-2: a manual selection names a roster person.

    An id that is not in the roster is an invalid edit value, not a database
    failure, so the caller gets a service error and the transaction writes
    nothing (the backend validates; a foreign-key error must not leak).
    """
    proposal_id = _make_proposal(conn, ptype=ptype)
    with pytest.raises(InvalidEditError):
        _edit(
            conn,
            proposal_id,
            ProposalEdit(assignees=(AssigneeSelection("Ghost", person_id=9999),)),
        )
    assert _count(conn, "proposal_revision") == 0
    state = conn.execute(
        "SELECT review_state FROM proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    assert str(state["review_state"]) == "PENDING"


def test_name_match_resolves_only_in_source_vertical(conn: sqlite3.Connection) -> None:
    """FR-PRP-10 / OD-REV-2: one active name match inside the source vertical."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(assignees=(AssigneeSelection("Arjun"),)))

    row = conn.execute(
        "SELECT ra.* FROM revision_assignee AS ra"
        " JOIN proposal_revision AS r ON r.id = ra.revision_id"
        " WHERE r.proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    assert str(row["resolution"]) == "RESOLVED"
    assert int(row["person_id"]) == VOLUNTEER3
    assert str(row["resolved_via"]) == "NAME_MATCH"


def test_name_match_ambiguous_stays_unresolved(conn: sqlite3.Connection) -> None:
    """FR-PRP-10: several matches stay unresolved so review must fix them."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(assignees=(AssigneeSelection("Neha"),)))

    row = conn.execute(
        "SELECT ra.* FROM revision_assignee AS ra"
        " JOIN proposal_revision AS r ON r.id = ra.revision_id"
        " WHERE r.proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    assert str(row["resolution"]) == "UNRESOLVED"
    assert row["person_id"] is None
    assert row["resolved_via"] is None


def test_name_match_absent_is_flagged(conn: sqlite3.Connection) -> None:
    """FR-PRP-10: no match flags the text; never a guessed person."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(assignees=(AssigneeSelection("Ravi"),)))

    row = conn.execute(
        "SELECT ra.* FROM revision_assignee AS ra"
        " JOIN proposal_revision AS r ON r.id = ra.revision_id"
        " WHERE r.proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    assert str(row["resolution"]) == "FLAGGED"
    assert row["person_id"] is None


def test_stale_edit_raises_and_writes_nothing(conn: sqlite3.Connection) -> None:
    """A9 / AC-19: a stale revision is rejected with the current values shown."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(description="First"))

    with pytest.raises(StaleRevisionError) as excinfo:
        _edit(conn, proposal_id, ProposalEdit(description="Second"), expected=0)

    assert excinfo.value.current_revision_number == 1
    assert excinfo.value.latest is not None
    assert excinfo.value.latest.description == "First"
    assert _count(conn, "proposal_revision") == 1


def test_no_change_edit_is_rejected(conn: sqlite3.Connection) -> None:
    """A10: a no-op edit writes no revision and fails clearly."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(NoChangesError):
        _edit(conn, proposal_id, ProposalEdit(description="Arrange the generator"))
    assert _count(conn, "proposal_revision") == 0


def test_cleared_description_is_stored_as_null(conn: sqlite3.Connection) -> None:
    """A10: reviewer input cleared to blank means NULL, distinct from missing."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(description="   "))

    revision = conn.execute(
        "SELECT description FROM proposal_revision WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    assert revision["description"] is None


def test_revision_stores_complete_effective_sets(conn: sqlite3.Connection) -> None:
    """A2: collections are copied forward intact, and an empty set is legitimate."""
    proposal_id = _make_proposal(
        conn, attributes=(("venue", "hall A"),), affected_vertical_ids=(1, 2)
    )
    _edit(conn, proposal_id, ProposalEdit(description="Edited"))
    revision = conn.execute(
        "SELECT id FROM proposal_revision WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    revision_id = int(revision["id"])
    copied_attributes = conn.execute(
        "SELECT attr_key, attr_value FROM revision_attribute WHERE revision_id = ?",
        (revision_id,),
    ).fetchall()
    copied_verticals = conn.execute(
        "SELECT vertical_id FROM revision_affected_vertical WHERE revision_id = ?"
        " ORDER BY vertical_id",
        (revision_id,),
    ).fetchall()
    assert [(row["attr_key"], row["attr_value"]) for row in copied_attributes] == [
        ("venue", "hall A")
    ]
    assert [int(row["vertical_id"]) for row in copied_verticals] == [1, 2]

    _edit(
        conn,
        proposal_id,
        ProposalEdit(attributes=None, affected_vertical_ids=None),
        expected=1,
    )
    latest = conn.execute(
        "SELECT id FROM proposal_revision WHERE proposal_id = ?"
        " ORDER BY revision_number DESC LIMIT 1",
        (proposal_id,),
    ).fetchone()
    latest_id = int(latest["id"])
    assert _count_where(conn, "revision_attribute", "revision_id", latest_id) == 0
    assert _count_where(conn, "revision_affected_vertical", "revision_id", latest_id) == 0


def _count_where(
    conn: sqlite3.Connection, table: str, column: str, value: int
) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (value,)
    ).fetchone()
    return int(row["n"])


def test_duplicate_persons_are_collapsed_in_revision(conn: sqlite3.Connection) -> None:
    """A14: first occurrence wins, so the schema index can never fire."""
    proposal_id = _make_proposal(conn)
    _edit(
        conn,
        proposal_id,
        ProposalEdit(
            assignees=(
                AssigneeSelection("Neha", person_id=VOLUNTEER),
                AssigneeSelection("Neha again", person_id=VOLUNTEER),
            )
        ),
    )
    rows = conn.execute(
        "SELECT ra.* FROM revision_assignee AS ra"
        " JOIN proposal_revision AS r ON r.id = ra.revision_id"
        " WHERE r.proposal_id = ?",
        (proposal_id,),
    ).fetchall()
    assert len(rows) == 1
    assert str(rows[0]["assignee_text"]) == "Neha"


def test_edit_never_deletes_original_proposal_assignees(conn: sqlite3.Connection) -> None:
    """OD-REV-3 / DR-03: the generated resolution remains as evidence."""
    proposal_id = _make_proposal(
        conn,
        assignees=(
            ("Neha", "RESOLVED", VOLUNTEER, "MENTION"),
            ("Isha", "RESOLVED", VOLUNTEER2, "NAME_MATCH"),
        ),
    )
    _edit(
        conn,
        proposal_id,
        ProposalEdit(assignees=(AssigneeSelection("Arjun", person_id=VOLUNTEER3),)),
    )
    rows = conn.execute(
        "SELECT assignee_text FROM proposal_assignee WHERE proposal_id = ? ORDER BY id",
        (proposal_id,),
    ).fetchall()
    assert [str(row["assignee_text"]) for row in rows] == ["Neha", "Isha"]


def test_edit_rejects_ineligible_manual_assignee(conn: sqlite3.Connection) -> None:
    """A6: eligibility is enforced when the selection is made, not only at accept."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _edit(
            conn,
            proposal_id,
            ProposalEdit(assignees=(AssigneeSelection("Ravi", person_id=VOL_V2),)),
        )
    assert excinfo.value.code == "ASSIGNEE_NOT_ELIGIBLE"
    assert _count(conn, "proposal_revision") == 0


def test_edit_ah_cannot_assign_another_ah(conn: sqlite3.Connection) -> None:
    """A6 / SRS §2.3: an AH names volunteers of the vertical, not other AHs."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _edit(
            conn,
            proposal_id,
            ProposalEdit(assignees=(AssigneeSelection("Sana", person_id=OTHER_AH),)),
        )
    assert excinfo.value.code == "ASSIGNEE_NOT_ELIGIBLE"


@pytest.mark.parametrize(
    "changes",
    [
        ProposalEdit(assignment_mode="MAYBE"),
        ProposalEdit(task_type="SIDEWAYS"),
        ProposalEdit(people_needed=0),
        ProposalEdit(people_needed_unlimited=2),
        ProposalEdit(responsible_vertical_id=999),
        ProposalEdit(assignees=("not a selection",)),
        ProposalEdit(attributes=(("", "x"),)),
        ProposalEdit(attributes=(("venue", " "),)),
    ],
)
def test_invalid_edit_values_are_rejected(
    conn: sqlite3.Connection, changes: ProposalEdit
) -> None:
    """The editable vocabulary is validated; nothing is written on rejection."""
    proposal_id = _make_proposal(conn)
    with pytest.raises(InvalidEditError):
        _edit(conn, proposal_id, changes)
    assert _count(conn, "proposal_revision") == 0


def test_edit_sets_attributes_and_affected_verticals(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn)
    _edit(
        conn,
        proposal_id,
        ProposalEdit(
            attributes=(("venue", "hall B"), ("time", None)),
            affected_vertical_ids=(3, 1, 3),
        ),
    )
    revision = conn.execute(
        "SELECT id FROM proposal_revision WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    revision_id = int(revision["id"])
    attributes = conn.execute(
        "SELECT attr_key, attr_value FROM revision_attribute WHERE revision_id = ?"
        " ORDER BY attr_key",
        (revision_id,),
    ).fetchall()
    verticals = conn.execute(
        "SELECT vertical_id FROM revision_affected_vertical WHERE revision_id = ?"
        " ORDER BY vertical_id",
        (revision_id,),
    ).fetchall()
    assert [(row["attr_key"], row["attr_value"]) for row in attributes] == [
        ("time", None),
        ("venue", "hall B"),
    ]
    assert [int(row["vertical_id"]) for row in verticals] == [1, 3]


def test_proposal_type_is_not_editable(conn: sqlite3.Connection) -> None:
    """OD-REV-4: ptype is immutable; the edit contract has no ptype member."""
    assert "ptype" not in {field.name for field in dataclasses.fields(ProposalEdit)}


# =========================================================================
# WS-5 — acceptance gating, decisions and entity materialization
# =========================================================================


def test_accept_task_materializes_entity_and_children(conn: sqlite3.Connection) -> None:
    """FR-REV-05/FR-TSK-03/FR-TSK-11/AC-07: one task, assignees, history, links."""
    proposal_id = _make_proposal(
        conn, attributes=(("venue", "hall A"),), affected_vertical_ids=(2,)
    )
    result = _accept(conn, proposal_id, note="looks right")

    assert result.created is True
    assert result.ptype == "TASK"
    assert result.approval_state == "NOT_REQUIRED"
    (task,) = conn.execute("SELECT * FROM task").fetchall()
    assert int(task["proposal_id"]) == proposal_id
    assert str(task["task_type"]) == "INTERNAL"
    assert int(task["source_vertical_id"]) == 1
    # PB-3/FR-TSK-09: INTERNAL task stores responsible_vertical_id = source_vertical_id NOT NULL
    assert int(task["responsible_vertical_id"]) == 1
    assert int(task["creator_id"]) == AH
    assert str(task["description"]) == "Arrange the generator"
    assert str(task["assignment_mode"]) == "EXPLICIT"
    # PB-3/FR-TSK-04: EXPLICIT tasks carry people_needed NULL
    assert task["people_needed"] is None
    assert int(task["people_needed_unlimited"]) == 0
    assert str(task["approval_state"]) == "NOT_REQUIRED"
    assert str(task["cancellation"]) == "ACTIVE"
    assert int(task["version"]) == 1

    (assignee,) = conn.execute("SELECT * FROM task_assignee").fetchall()
    assert int(assignee["person_id"]) == VOLUNTEER
    assert str(assignee["agreement"]) == "AWAITING"
    assert assignee["agreed_at"] is None
    assert str(assignee["work_status"]) == "NOT_STARTED"
    assert int(assignee["cancelled"]) == 0

    (history,) = conn.execute("SELECT * FROM task_history").fetchall()
    assert str(history["field_name"]) == "TASK_CREATED"
    assert history["old_value"] is None
    assert str(history["new_value"]) == "ACTIVE"
    assert int(history["actor_person_id"]) == AH
    assert str(history["actor_role"]) == "AH"
    assert int(history["actor_vertical_id"]) == 1
    assert str(history["reason"]) == "PROPOSAL_ACCEPTED"

    attribute = conn.execute("SELECT * FROM task_attribute").fetchone()
    assert (attribute["attr_key"], attribute["attr_value"]) == ("venue", "hall A")
    affected = conn.execute("SELECT vertical_id FROM task_affected_vertical").fetchone()
    assert int(affected["vertical_id"]) == 2

    decision = conn.execute("SELECT * FROM proposal_decision").fetchone()
    assert str(decision["decision"]) == "ACCEPTED"
    assert int(decision["decided_by"]) == AH
    assert str(decision["note"]) == "looks right"
    state = conn.execute("SELECT review_state FROM proposal WHERE id = ?", (proposal_id,))
    assert str(state.fetchone()["review_state"]) == "ACCEPTED"
    assert _count(conn, "announcement") == 0


def test_creator_is_sender_when_admin_accepts(conn: sqlite3.Connection) -> None:
    """OD-REV-6: creator = sender; the Admin is the recorded actor."""
    proposal_id = _make_proposal(conn)
    _accept(conn, proposal_id, actor=ADMIN)

    task = conn.execute("SELECT * FROM task").fetchone()
    decision = conn.execute("SELECT * FROM proposal_decision").fetchone()
    history = conn.execute("SELECT * FROM task_history").fetchone()
    assert int(task["creator_id"]) == AH
    assert int(decision["decided_by"]) == ADMIN
    assert int(history["actor_person_id"]) == ADMIN
    assert str(history["actor_role"]) == "Admin"


def test_accept_uses_latest_edited_values(conn: sqlite3.Connection) -> None:
    """FR-REV-01: accepting an edited proposal creates the entity from edits."""
    proposal_id = _make_proposal(conn)
    _edit(
        conn,
        proposal_id,
        ProposalEdit(
            description="Edited task text",
            assignees=(AssigneeSelection("Arjun", person_id=VOLUNTEER3),),
            attributes=(("venue", "gate"),),
        ),
    )
    _accept(conn, proposal_id, expected=1)

    task = conn.execute("SELECT * FROM task").fetchone()
    (assignee,) = conn.execute("SELECT * FROM task_assignee").fetchall()
    assert str(task["description"]) == "Edited task text"
    assert int(assignee["person_id"]) == VOLUNTEER3


@pytest.mark.parametrize("resolution", ["UNRESOLVED", "FLAGGED"])
def test_accept_blocked_by_unresolved_or_flagged_assignee(
    conn: sqlite3.Connection, resolution: str
) -> None:
    """FR-REV-06 / AC-06: no accept while an assignee is unresolved/flagged."""
    proposal_id = _make_proposal(
        conn,
        assignees=(("maybe", resolution, None, None),),
    )
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)

    assert excinfo.value.code == "UNRESOLVED_ASSIGNEES"
    _assert_no_entity_writes(conn)
    state = conn.execute("SELECT review_state FROM proposal WHERE id = ?", (proposal_id,))
    assert str(state.fetchone()["review_state"]) == "PENDING"


def test_accept_blocked_by_blank_description(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn, description=None)
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "REQUIRED_FIELD_UNFILLED"
    assert excinfo.value.details == ("description",)
    _assert_no_entity_writes(conn)


def test_accept_blocked_open_without_people_needed(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn, assignment_mode="OPEN", people_needed=None)
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "REQUIRED_FIELD_UNFILLED"
    assert excinfo.value.details == ("people_needed",)


def test_accept_open_unlimited_task(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(
        conn, assignment_mode="OPEN", people_needed=None, people_needed_unlimited=1
    )
    _accept(conn, proposal_id)
    task = conn.execute("SELECT * FROM task").fetchone()
    assert task["people_needed"] is None
    assert int(task["people_needed_unlimited"]) == 1


def test_accept_open_task_with_positive_count(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn, assignment_mode="OPEN", people_needed=5)
    _accept(conn, proposal_id)
    task = conn.execute("SELECT * FROM task").fetchone()
    assert int(task["people_needed"]) == 5
    assert int(task["people_needed_unlimited"]) == 0


def test_accept_blocked_explicit_without_assignees(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn, assignees=())
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "REQUIRED_FIELD_UNFILLED"
    assert excinfo.value.details == ("assignees",)


def test_accept_blocked_mode_assignee_contradiction(conn: sqlite3.Connection) -> None:
    """FR-PRP-08: OPEN never carries a named person."""
    proposal_id = _make_proposal(
        conn,
        assignment_mode="OPEN",
        people_needed=2,
        assignees=(("Neha", "RESOLVED", VOLUNTEER, "MENTION"),),
    )
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "MODE_ASSIGNEE_CONTRADICTION"


def test_accept_blocked_cross_vertical_without_responsible(
    conn: sqlite3.Connection,
) -> None:
    proposal_id = _make_proposal(conn, task_type="CROSS_VERTICAL")
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "REQUIRED_FIELD_UNFILLED"
    assert excinfo.value.details == ("responsible_vertical_id",)


def test_accept_blocked_cross_vertical_same_vertical(conn: sqlite3.Connection) -> None:
    """A3: CROSS_VERTICAL requires responsible != source so 0006 cannot fire."""
    proposal_id = _make_proposal(
        conn, task_type="CROSS_VERTICAL", responsible_vertical_id=1
    )
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "CROSS_VERTICAL_VERTICAL_CONTRADICTION"


def test_accept_blocked_internal_other_vertical(conn: sqlite3.Connection) -> None:
    """A3: INTERNAL may keep the source vertical or NULL, never another one."""
    proposal_id = _make_proposal(conn, responsible_vertical_id=2)
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "INTERNAL_VERTICAL_CONTRADICTION"


def test_accept_blocked_task_in_none_mode(conn: sqlite3.Connection) -> None:
    """A3: 0006's assignment_mode CHECK can never fire from the accept path."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(assignment_mode="NONE", assignees=None))
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id, expected=1)
    assert excinfo.value.code == "TASK_MODE_CONTRADICTION"
    _assert_no_entity_writes(conn)


def test_accept_collapses_duplicate_assignees(conn: sqlite3.Connection) -> None:
    """A14: two generated rows naming the same person insert one task_assignee."""
    proposal_id = _make_proposal(
        conn,
        assignees=(
            ("Neha", "RESOLVED", VOLUNTEER, "MENTION"),
            ("Neha again", "RESOLVED", VOLUNTEER, "NAME_MATCH"),
        ),
    )
    _accept(conn, proposal_id)

    rows = conn.execute("SELECT person_id FROM task_assignee").fetchall()
    assert [int(row["person_id"]) for row in rows] == [VOLUNTEER]
    assigned = [
        row
        for row in _notifications(conn)
        if str(row["event_type"]) == "TASK_ASSIGNED"
    ]
    assert len(assigned) == 1


@pytest.mark.parametrize("person_id", [OTHER_AH, INACTIVE, VOL_V2])
def test_accept_blocked_ineligible_assignee(
    conn: sqlite3.Connection, person_id: int
) -> None:
    """A6 / FR-AUT-07: role pool, active status and vertical pool are enforced."""
    proposal_id = _make_proposal(
        conn, assignees=(("Someone", "RESOLVED", person_id, "NAME_MATCH"),)
    )
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "ASSIGNEE_NOT_ELIGIBLE"
    _assert_no_entity_writes(conn)


def test_accept_stale_revision_writes_nothing(conn: sqlite3.Connection) -> None:
    """A9: accept has edit's stale check and writes nothing on mismatch."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(description="Revised"))

    with pytest.raises(StaleRevisionError):
        _accept(conn, proposal_id, expected=0)

    _assert_no_entity_writes(conn)
    state = conn.execute("SELECT review_state FROM proposal WHERE id = ?", (proposal_id,))
    assert str(state.fetchone()["review_state"]) == "EDITED"


def test_accept_concurrent_one_winner(tmp_path: Path) -> None:
    """NFR-CON-02 / FR-REV-05 / AC-18: two simultaneous accepts yield one task."""
    path = tmp_path / "concurrent.db"
    setup = connect(path)
    try:
        apply_migrations(setup)
        _seed_world(setup)
        proposal_id = _make_proposal(setup)
    finally:
        setup.close()

    results: list[AcceptResult] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        connection = connect(path)
        try:
            barrier.wait()
            results.append(
                accept_proposal(
                    connection,
                    proposal_id,
                    actor_person_id=AH,
                    expected_revision_number=None,
                )
            )
        except BaseException as error:  # noqa: BLE001 - surfaced by assertion below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(result.created for result in results) == [False, True]
    assert len({result.entity_id for result in results}) == 1

    check = connect(path)
    try:
        assert _count(check, "task") == 1
        assert _count(check, "proposal_decision") == 1
    finally:
        check.close()


def test_reject_records_decision_and_keeps_history(conn: sqlite3.Connection) -> None:
    """FR-REV-07 / AC-04: rejection keeps proposal, revisions and note."""
    proposal_id = _make_proposal(conn)
    _edit(conn, proposal_id, ProposalEdit(description="Still not right"))
    result = reject_proposal(
        conn, proposal_id, actor_person_id=AH, note="  wrong venue  "
    )

    assert result.created is True
    assert result.note == "wrong venue"
    decision = conn.execute("SELECT * FROM proposal_decision").fetchone()
    assert int(decision["id"]) == result.decision_id
    assert str(decision["decision"]) == "REJECTED"
    assert int(decision["decided_by"]) == AH
    assert str(decision["note"]) == "wrong venue"
    state = conn.execute("SELECT review_state FROM proposal WHERE id = ?", (proposal_id,))
    assert str(state.fetchone()["review_state"]) == "REJECTED"
    assert _count(conn, "proposal_revision") == 1
    assert _count(conn, "task") == 0
    assert _count(conn, "announcement") == 0


def test_reject_blank_note_is_stored_as_null(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn)
    result = reject_proposal(conn, proposal_id, actor_person_id=AH, note="   ")
    assert result.note is None
    decision = conn.execute("SELECT note FROM proposal_decision").fetchone()
    assert decision["note"] is None


def test_reject_after_accept_is_terminal(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn)
    _accept(conn, proposal_id)
    with pytest.raises(TerminalStateError):
        reject_proposal(conn, proposal_id, actor_person_id=AH)
    assert _count(conn, "proposal_decision") == 1


def test_accept_announcement_head_scope(conn: sqlite3.Connection) -> None:
    """FR-REV-10 / OD-REV-5: Head announcement targets the source vertical."""
    proposal_id = _make_proposal(
        conn,
        sender_person_id=HEAD,
        ptype="ANNOUNCEMENT",
        assignment_mode="NONE",
        assignees=(),
    )
    result = _accept(conn, proposal_id, actor=HEAD)

    assert result.ptype == "ANNOUNCEMENT"
    assert result.approval_state is None
    announcement = conn.execute("SELECT * FROM announcement").fetchone()
    assert int(announcement["proposal_id"]) == proposal_id
    assert int(announcement["accepted_by"]) == HEAD
    assert int(announcement["audience_vertical_id"]) == 1
    # PB-6/FR-REV-10: audience_scope replaced by audience_type and audience_ah_person_id
    assert str(announcement["audience_type"]) == "VERTICAL"
    assert announcement["audience_ah_person_id"] is None
    assert _count(conn, "task") == 0
    assert _count(conn, "task_assignee") == 0


def test_accept_announcement_ah_scope_creates_no_task_or_notification(
    conn: sqlite3.Connection,
) -> None:
    """FR-REV-10 / A16: AH announcement has no task and stages no notification."""
    proposal_id = _make_proposal(
        conn, ptype="ANNOUNCEMENT", assignment_mode="NONE", assignees=()
    )
    result = _accept(conn, proposal_id, actor=AH)

    announcement = conn.execute("SELECT * FROM announcement").fetchone()
    # PB-6/FR-REV-10: audience_scope replaced by audience_type and audience_ah_person_id snapshot
    assert str(announcement["audience_type"]) == "AH_VOLUNTEERS"
    assert int(announcement["audience_ah_person_id"]) == AH
    assert int(announcement["audience_vertical_id"]) == 1
    assert result.notification_count == 0
    assert _count(conn, "notification") == 0
    assert _count(conn, "task") == 0


def test_announcement_scope_follows_sender_not_admin(conn: sqlite3.Connection) -> None:
    """OD-REV-5/A5: an Admin accepting for an AH keeps the AH scope."""
    proposal_id = _make_proposal(
        conn, ptype="ANNOUNCEMENT", assignment_mode="NONE", assignees=()
    )
    _accept(conn, proposal_id, actor=ADMIN)
    announcement = conn.execute("SELECT * FROM announcement").fetchone()
    assert int(announcement["accepted_by"]) == ADMIN
    # PB-6/OD-REV-5: Admin accepting AH announcement keeps AH_VOLUNTEERS and sender AH snapshot
    assert str(announcement["audience_type"]) == "AH_VOLUNTEERS"
    assert int(announcement["audience_ah_person_id"]) == AH


@pytest.mark.parametrize(
    ("assignment_mode", "generated", "changes"),
    [
        (
            "EXPLICIT",
            _ASSIGNEE_DEFAULT,
            ProposalEdit(description="Edited announcement"),
        ),
        (
            "NONE",
            (),
            ProposalEdit(assignees=(AssigneeSelection("Neha", person_id=VOLUNTEER),)),
        ),
    ],
    ids=["generated-assignees", "new-assignees"],
)
def test_edit_announcement_with_assignees_is_blocked(
    conn: sqlite3.Connection,
    assignment_mode: str,
    generated: tuple[tuple[str, str, int | None, str | None], ...],
    changes: ProposalEdit,
) -> None:
    """FR-REV-10 / plan 4.5: an announcement has no assignees.

    Saving an edit that would leave the announcement carrying assignees is a
    mode/assignee contradiction - whether the assignees were generated or
    supplied by the reviewer - and the edit writes nothing.
    """
    proposal_id = _make_proposal(
        conn,
        ptype="ANNOUNCEMENT",
        assignment_mode=assignment_mode,
        assignees=generated,
    )

    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _edit(conn, proposal_id, changes)

    assert excinfo.value.code == "MODE_ASSIGNEE_CONTRADICTION"
    assert _count(conn, "proposal_revision") == 0
    state = conn.execute(
        "SELECT review_state FROM proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    assert str(state["review_state"]) == "PENDING"


def test_edit_announcement_can_clear_generated_assignees(
    conn: sqlite3.Connection,
) -> None:
    """Plan 4.5: the reviewer can always remove the contradiction in one edit,
    so a bad generated announcement is repairable before acceptance."""
    proposal_id = _make_proposal(
        conn,
        ptype="ANNOUNCEMENT",
        assignment_mode="EXPLICIT",
        assignees=_ASSIGNEE_DEFAULT,
    )

    result = _edit(
        conn,
        proposal_id,
        ProposalEdit(assignment_mode="NONE", assignees=None),
    )

    assert result.revision_number == 1  # type: ignore[attr-defined]
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM revision_assignee"
        ).fetchone()["n"]
        == 0
    )


def test_accept_announcement_with_assignees_is_blocked(
    conn: sqlite3.Connection,
) -> None:
    """FR-REV-10 / plan 4.5: acceptance cannot create an announcement that
    still carries assignees; no entity or decision is written."""
    proposal_id = _make_proposal(
        conn,
        ptype="ANNOUNCEMENT",
        assignment_mode="EXPLICIT",
        assignees=_ASSIGNEE_DEFAULT,
    )

    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)

    assert excinfo.value.code == "MODE_ASSIGNEE_CONTRADICTION"
    _assert_no_entity_writes(conn)
    state = conn.execute(
        "SELECT review_state FROM proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    assert str(state["review_state"]) == "PENDING"


@pytest.mark.parametrize(
    "sender_person_id", [VOLUNTEER3, ADMIN, None], ids=["volunteer", "admin", "missing"]
)
def test_unsupported_sender_role_is_fail_closed(
    conn: sqlite3.Connection, sender_person_id: int | None
) -> None:
    """A13: any sender token other than exactly AH/Head blocks before any write."""
    proposal_id = _make_proposal(conn, sender_person_id=sender_person_id)
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id, actor=ADMIN)
    assert excinfo.value.code == "SENDER_ROLE_UNSUPPORTED"
    _assert_no_entity_writes(conn)


@pytest.mark.parametrize(
    ("person_id", "role"),
    [(AH, "AH"), (HEAD, "Head")],
)
def test_sender_role_matching_uses_the_canonical_role(
    conn: sqlite3.Connection, person_id: int, role: str
) -> None:
    """PB-5/O7-6: the strict CHECK stores canonical roles; the roster loader,
    not this layer, normalizes case before the row is written."""
    proposal_id = _make_proposal(conn, sender_person_id=person_id)
    result = _accept(conn, proposal_id, actor=person_id)
    assert result.created is True
    history = conn.execute("SELECT actor_role FROM task_history").fetchone()
    assert str(history["actor_role"]) == role


# =========================================================================
# WS-6 — cross-vertical tasks and notification staging
# =========================================================================


def test_ah_cross_vertical_task_is_pending_and_requests_approval(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-03 / A4/A8/A15: hidden task, request to every AH and Head."""
    proposal_id = _make_proposal(
        conn,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignees=(("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),),
    )
    result = _accept(conn, proposal_id, request_message="need two hands")

    assert result.approval_state == "PENDING"
    task = conn.execute("SELECT * FROM task").fetchone()
    assert str(task["approval_state"]) == "PENDING"
    assert int(task["responsible_vertical_id"]) == 2
    request = conn.execute("SELECT * FROM approval_request").fetchone()
    assert int(request["task_id"]) == result.entity_id
    assert int(request["requested_by"]) == AH
    assert str(request["request_message"]) == "need two hands"
    recipients = conn.execute(
        "SELECT person_id FROM approval_recipient ORDER BY person_id"
    ).fetchall()
    assert [int(row["person_id"]) for row in recipients] == [AH_V2, HEAD_V2, HEAD_V2_B, AH_V2_B]
    requests = [
        row
        for row in _notifications(conn)
        if str(row["event_type"]) == "CROSS_VERTICAL_REQUEST"
    ]
    assert sorted(int(row["recipient_person_id"]) for row in requests) == [
        AH_V2,
        HEAD_V2,
        HEAD_V2_B,
        AH_V2_B,
    ]


def test_approval_requested_by_is_sender_when_admin_accepts(
    conn: sqlite3.Connection,
) -> None:
    """A15: requested_by is the requester, never the accepting Admin."""
    proposal_id = _make_proposal(
        conn,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignees=(("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),),
    )
    _accept(conn, proposal_id, actor=ADMIN)
    request = conn.execute("SELECT * FROM approval_request").fetchone()
    assert int(request["requested_by"]) == AH
    task = conn.execute("SELECT * FROM task").fetchone()
    assert int(task["creator_id"]) == AH


def test_approval_recipient_snapshot_excludes_requester(
    conn: sqlite3.Connection,
) -> None:
    """A4/A15: a requester who is an AH of the target vertical is excluded."""
    proposal_id = _make_proposal(
        conn,
        sender_person_id=AH_V2,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignees=(("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),),
    )
    _accept(conn, proposal_id, actor=AH_V2)

    recipients = conn.execute(
        "SELECT person_id FROM approval_recipient ORDER BY person_id"
    ).fetchall()
    assert [int(row["person_id"]) for row in recipients] == [HEAD_V2, HEAD_V2_B, AH_V2_B]
    assert AH_V2 not in [int(row["person_id"]) for row in recipients]


def test_empty_approval_recipient_snapshot_blocks_accept(
    conn: sqlite3.Connection,
) -> None:
    """A4 / OD-REV-7: no hidden task that can never be approved."""
    proposal_id = _make_proposal(
        conn,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=3,
        assignees=(("Tara", "RESOLVED", VOL_V3, "NAME_MATCH"),),
    )
    with pytest.raises(AcceptanceBlockedError) as excinfo:
        _accept(conn, proposal_id)
    assert excinfo.value.code == "NO_APPROVAL_RECIPIENTS"
    _assert_no_entity_writes(conn)
    assert _count(conn, "approval_request") == 0


def test_head_cross_vertical_task_goes_live_and_notifies_heads(
    conn: sqlite3.Connection,
) -> None:
    """FR-XV-02 / A8: Head cross-vertical task is NOT_REQUIRED and live."""
    proposal_id = _make_proposal(
        conn,
        sender_person_id=HEAD,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignees=(("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),),
    )
    result = _accept(conn, proposal_id, actor=HEAD)

    assert result.approval_state == "NOT_REQUIRED"
    assert _count(conn, "approval_request") == 0
    event_types = [str(row["event_type"]) for row in _notifications(conn)]
    assert event_types.count("HEAD_CROSS_VERTICAL_TASK") == 2
    assert event_types.count("TASK_ASSIGNED") == 1
    head_recipients = sorted(
        int(row["recipient_person_id"])
        for row in _notifications(conn)
        if str(row["event_type"]) == "HEAD_CROSS_VERTICAL_TASK"
    )
    assert head_recipients == [HEAD_V2, HEAD_V2_B]


def test_hidden_pending_task_stages_no_assignment_or_availability_rows(
    conn: sqlite3.Connection,
) -> None:
    """A8/FR-XV-03: a hidden PENDING task is not announced to volunteers."""
    proposal_id = _make_proposal(
        conn, task_type="CROSS_VERTICAL", responsible_vertical_id=2,
        assignment_mode="OPEN", people_needed=5,
    )
    _accept(conn, proposal_id)

    event_types = [str(row["event_type"]) for row in _notifications(conn)]
    assert "TASK_ASSIGNED" not in event_types
    assert "OPEN_TASK_AVAILABLE" not in event_types
    assert event_types.count("CROSS_VERTICAL_REQUEST") == 4


def test_hidden_pending_explicit_task_has_no_task_assigned_row(
    conn: sqlite3.Connection,
) -> None:
    proposal_id = _make_proposal(
        conn,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignees=(("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),),
    )
    _accept(conn, proposal_id)
    event_types = [str(row["event_type"]) for row in _notifications(conn)]
    assert "TASK_ASSIGNED" not in event_types
    assert event_types.count("CROSS_VERTICAL_REQUEST") == 4


def test_open_internal_task_targets_source_vertical_volunteers(
    conn: sqlite3.Connection,
) -> None:
    proposal_id = _make_proposal(
        conn, sender_person_id=HEAD, assignment_mode="OPEN", people_needed=3
    )
    _accept(conn, proposal_id, actor=HEAD)

    recipients = sorted(
        int(row["recipient_person_id"])
        for row in _notifications(conn)
        if str(row["event_type"]) == "OPEN_TASK_AVAILABLE"
    )
    assert recipients == [VOLUNTEER, VOLUNTEER2, VOLUNTEER3, DUPLICATE_NAME]


def test_open_cross_vertical_task_targets_responsible_volunteers(
    conn: sqlite3.Connection,
) -> None:
    proposal_id = _make_proposal(
        conn,
        sender_person_id=HEAD,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignment_mode="OPEN",
        people_needed=3,
    )
    _accept(conn, proposal_id, actor=HEAD)

    recipients = sorted(
        int(row["recipient_person_id"])
        for row in _notifications(conn)
        if str(row["event_type"]) == "OPEN_TASK_AVAILABLE"
    )
    assert recipients == [VOL_V2, VOL_V2_B]


def test_no_announcement_notification_rows(conn: sqlite3.Connection) -> None:
    """A16: Phase 6 stages no per-person ANNOUNCEMENT notifications."""
    proposal_id = _make_proposal(
        conn, ptype="ANNOUNCEMENT", assignment_mode="NONE", assignees=()
    )
    _accept(conn, proposal_id)
    assert _count(conn, "notification") == 0


def test_notification_rows_are_pending_and_single_anchored(
    conn: sqlite3.Connection,
) -> None:
    """A8: dedupe keys are unique, state is PENDING and exactly one FK anchors."""
    proposal_id = _make_proposal(
        conn,
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        assignees=(("Ravi", "RESOLVED", VOL_V2, "NAME_MATCH"),),
    )
    _accept(conn, proposal_id)

    rows = _notifications(conn)
    assert len(rows) == 4
    assert len({str(row["dedupe_key"]) for row in rows}) == len(rows)
    for row in rows:
        assert str(row["delivery_state"]) == "PENDING"
        anchors = [
            row["proposal_id"],
            row["task_id"],
            row["approval_request_id"],
            row["announcement_id"],
        ]
        assert sum(value is not None for value in anchors) == 1
        assert str(row["event_type"]) == "CROSS_VERTICAL_REQUEST"
        assert str(row["dedupe_key"]).startswith("CROSS_VERTICAL_REQUEST:")


def test_repeat_accept_stages_no_new_notifications(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn)
    _accept(conn, proposal_id)
    before = _count(conn, "notification")
    result = _accept(conn, proposal_id)
    assert result.notification_count == 0
    assert _count(conn, "notification") == before


# =========================================================================
# FR-PRP-17 — notification hook and recovery sweep (A7/A18)
# =========================================================================


def test_notify_proposal_ready_inserts_reviewer_row(conn: sqlite3.Connection) -> None:
    """FR-PRP-17: a valid stored proposal notifies its sender and no one else."""
    proposal_id = _make_proposal(conn)
    assert notify_proposal_ready(conn, proposal_id) == proposal_id

    (row,) = _notifications(conn)
    assert str(row["event_type"]) == "PROPOSAL_READY"
    assert int(row["recipient_person_id"]) == AH
    assert str(row["dedupe_key"]) == f"PROPOSAL_READY:{proposal_id}"
    assert int(row["proposal_id"]) == proposal_id
    assert str(row["delivery_state"]) == "PENDING"


def test_notify_proposal_ready_is_idempotent(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn)
    assert notify_proposal_ready(conn, proposal_id) == proposal_id
    assert notify_proposal_ready(conn, proposal_id) is None
    assert _count(conn, "notification") == 1


def test_notify_skips_structurally_invalid_proposal(conn: sqlite3.Connection) -> None:
    """A18: structurally_valid = 0 produces no notification."""
    proposal_id = _make_proposal(conn, structurally_valid=0)
    assert notify_proposal_ready(conn, proposal_id) is None
    assert _count(conn, "notification") == 0


def test_notify_skips_missing_proposal(conn: sqlite3.Connection) -> None:
    assert notify_proposal_ready(conn, 9999) is None


def test_notify_skips_proposal_without_sender(conn: sqlite3.Connection) -> None:
    proposal_id = _make_proposal(conn, sender_person_id=None)
    assert notify_proposal_ready(conn, proposal_id) is None


def test_sweep_recovers_only_valid_unnotified_proposals(
    conn: sqlite3.Connection,
) -> None:
    missing = _make_proposal(conn)
    invalid = _make_proposal(conn, structurally_valid=0)
    already = _make_proposal(conn)
    notify_proposal_ready(conn, already)
    without_sender = _make_proposal(conn, sender_person_id=None)

    assert sweep_unnotified_proposals(conn) == (missing,)
    assert _count(conn, "notification") == 2
    assert sweep_unnotified_proposals(conn) == ()
    assert _count(conn, "notification") == 2

    notified = {int(row["proposal_id"]) for row in _notifications(conn)}
    assert notified == {missing, already}
    assert invalid not in notified
    assert without_sender not in notified


# =========================================================================
# FR-REV-08 — evaluation export
# =========================================================================


def test_eval_export_includes_every_edited_and_rejected_proposal(
    conn: sqlite3.Connection,
) -> None:
    """FR-REV-08: rejected and edited proposals are labelled evaluation data.

    Selection is revision-based, so an edited-then-accepted proposal is
    exported as well: its edits are exactly the labels the evaluator needs,
    and its final state is ACCEPTED. A rejected proposal is exported even
    when it was never edited, because the rejection itself is the label.
    An untouched accepted proposal carries no label and is not exported.
    """
    edited = _make_proposal(conn)
    _edit(conn, edited, ProposalEdit(description="Edited for evaluation"))
    edited_accepted = _make_proposal(conn)
    _edit(conn, edited_accepted, ProposalEdit(description="Edited then accepted"))
    _accept(conn, edited_accepted, expected=1)
    edited_rejected = _make_proposal(conn)
    _edit(conn, edited_rejected, ProposalEdit(description="Edited then rejected"))
    reject_proposal(conn, edited_rejected, actor_person_id=AH, note="nope")
    rejected_direct = _make_proposal(conn)
    reject_proposal(conn, rejected_direct, actor_person_id=AH)
    pending = _make_proposal(conn)
    accepted_clean = _make_proposal(conn)
    _accept(conn, accepted_clean)

    exported = {
        entry.proposal_id: entry
        for entry in list_reviewed_proposals_for_eval(conn, actor_person_id=ADMIN)
    }

    assert set(exported) == {
        edited,
        edited_accepted,
        edited_rejected,
        rejected_direct,
    }
    assert exported[edited].review_state == "EDITED"
    assert exported[edited].decision is None
    assert exported[edited].revisions[0].changed_fields == ("description",)
    assert exported[edited_accepted].review_state == "ACCEPTED"
    assert exported[edited_accepted].decision == "ACCEPTED"
    assert exported[edited_rejected].review_state == "REJECTED"
    assert exported[edited_rejected].decision == "REJECTED"
    assert exported[edited_rejected].note == "nope"
    assert exported[rejected_direct].review_state == "REJECTED"
    assert exported[rejected_direct].decision == "REJECTED"
    assert exported[rejected_direct].revisions == ()
    assert pending not in exported
    assert accepted_clean not in exported


def test_eval_export_row_carries_revisions_and_generated_final_delta(
    conn: sqlite3.Connection,
) -> None:
    """FR-REV-08/FR-REV-09: each row exposes per-revision changed fields and
    the generated-versus-final delta, so the export is usable as labelled
    data without recomputing the comparison per consumer."""
    proposal_id = _make_proposal(
        conn,
        description="Original generator line",
        assignment_mode="OPEN",
        people_needed=2,
    )
    _edit(
        conn,
        proposal_id,
        ProposalEdit(description="Edited line", people_needed=4),
    )
    _accept(conn, proposal_id, expected=1)

    (entry,) = list_reviewed_proposals_for_eval(conn, actor_person_id=ADMIN)

    assert entry.proposal_id == proposal_id
    assert entry.review_state == "ACCEPTED"
    assert entry.decision == "ACCEPTED"
    assert entry.revisions[0].revision_number == 1
    assert entry.revisions[0].changed_fields == ("description", "people_needed")
    assert {
        change.field: (change.generated, change.final)
        for change in entry.generated_final_delta
    } == {
        "description": ("Original generator line", "Edited line"),
        "people_needed": (2, 4),
    }


def test_eval_export_direct_rejection_has_empty_delta(conn: sqlite3.Connection) -> None:
    """FR-REV-08: a rejection without an edit still has labels, and its
    generated-versus-final delta is empty because nothing changed."""
    proposal_id = _make_proposal(conn)
    reject_proposal(conn, proposal_id, actor_person_id=AH)

    (entry,) = list_reviewed_proposals_for_eval(conn, actor_person_id=ADMIN)

    assert entry.revisions == ()
    assert entry.generated_final_delta == ()
    assert entry.decision == "REJECTED"
    assert entry.decided_by == AH


def test_eval_export_admin_actor_is_allowed(conn: sqlite3.Connection) -> None:
    """FR-REV-08/FR-AUT-01: the evaluation surface is an Admin-only internal
    read; the configured Admin token is accepted and records no denial."""
    proposal_id = _make_proposal(conn)
    reject_proposal(conn, proposal_id, actor_person_id=AH)

    (entry,) = list_reviewed_proposals_for_eval(conn, actor_person_id=ADMIN)

    assert entry.proposal_id == proposal_id
    assert _denials(conn) == []


def test_eval_export_non_admin_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-06: a non-Admin caller is rejected and the attempt is recorded
    in authorization_denial before the exception (owner amendment A17)."""
    proposal_id = _make_proposal(conn)
    reject_proposal(conn, proposal_id, actor_person_id=AH)

    with pytest.raises(ReviewAuthorizationError) as excinfo:
        list_reviewed_proposals_for_eval(conn, actor_person_id=AH)

    assert str(excinfo.value) == "NOT_ADMIN"
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == AH
    assert str(denial["operation"]) == "EVAL_EXPORT"
    assert str(denial["subject_type"]) == "EVALUATION"
    assert denial["subject_id"] is None
    assert str(denial["reason"]) == "NOT_ADMIN"


def test_eval_export_inactive_admin_is_denied_and_recorded(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-05/FR-AUT-06: authorization uses the current active state; a
    deactivated Admin is rejected and the attempt is recorded."""
    _person(conn, 17, "Retired Admin", "Admin", 1, active=0)

    with pytest.raises(ReviewAuthorizationError) as excinfo:
        list_reviewed_proposals_for_eval(conn, actor_person_id=17)

    assert str(excinfo.value) == "ACTOR_INACTIVE"
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == 17
    assert str(denial["operation"]) == "EVAL_EXPORT"
    assert str(denial["reason"]) == "ACTOR_INACTIVE"


def test_eval_export_unknown_actor_is_denied_with_null_actor(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-06/DR-04: an unknown caller has no roster row, so the denial
    stores the actor as missing rather than guessing a person."""
    with pytest.raises(ReviewAuthorizationError) as excinfo:
        list_reviewed_proposals_for_eval(conn, actor_person_id=9999)

    assert str(excinfo.value) == "ACTOR_NOT_FOUND"
    (denial,) = _denials(conn)
    assert denial["actor_person_id"] is None
    assert str(denial["operation"]) == "EVAL_EXPORT"
    assert str(denial["reason"]) == "ACTOR_NOT_FOUND"


def test_eval_export_admin_token_is_configurable(conn: sqlite3.Connection) -> None:
    """PB-5/FR-AUT-04: Admin token comes from configuration with canonical person role."""
    _person(conn, 18, "Lead", "Head", 1)
    proposal_id = _make_proposal(conn)
    reject_proposal(conn, proposal_id, actor_person_id=AH)

    (entry,) = list_reviewed_proposals_for_eval(
        conn,
        actor_person_id=18,
        policy=ReviewPolicy(admin_role_tokens=("Head",)),
    )

    assert entry.proposal_id == proposal_id
