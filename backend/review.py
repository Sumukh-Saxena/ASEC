"""Proposal review service (Phase 6).

Implements SRS FR-REV-01..10, FR-PRP-17, FR-AUT-06 and the owner decisions in
``docs/DECISIONS.md`` section A (OD-REV-1..7) with amendments A2..A18 from
``docs/PHASE6_PLAN.md``.

Boundary rules enforced here:

* The backend is the only authority. Every public operation authenticates the
  caller against the database first (FR-AUT-01/02), and an unauthorized caller
  is recorded in ``authorization_denial`` before ``ReviewAuthorizationError``
  is raised (FR-AUT-06, A17).
* A proposal never becomes a task or announcement without an explicit human
  accept (FR-PRP-16, BR-10).
* Edits append an immutable ``proposal_revision`` snapshot; the generated
  proposal content columns are never overwritten (FR-REV-03, AI-03, DR-03).
* Accept/reject is one ``BEGIN IMMEDIATE`` transaction (NFR-CON-02) and the
  accept materializes exactly one ``task`` or ``announcement`` (FR-REV-05).
* Notification rows are staged only (no delivery; Phase 9 owns transport) and
  a structurally invalid proposal produces none (FR-PRP-17, A18).

Import direction is one-way: ``backend.proposals`` may import this module to
wire the FR-PRP-17 hook (A7); this module never imports ``backend.proposals``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.authorization import (
    AuthorizationError,
    require_active_actor,
)
from backend.authorization import (
    record_denial as _record_denial,
)
from backend.db.connection import transaction
from backend.notification_events import (
    EVENT_CROSS_VERTICAL_REQUEST,
    EVENT_HEAD_CROSS_VERTICAL_TASK,
    EVENT_OPEN_TASK_AVAILABLE,
    EVENT_PROPOSAL_READY,
    EVENT_TASK_ASSIGNED,
    approval_request_key,
    proposal_ready_key,
    task_assignee_key,
)
from backend.notification_events import (
    stage_notification as _insert_notification,
)

DEFAULT_ADMIN_ROLE_TOKENS = ("Admin",)
ADMIN_ROLES_CONFIG_KEY = "ADMIN_ROLES"

OPERATION_PROPOSAL_REVIEW = "PROPOSAL_REVIEW"
OPERATION_EVAL_EXPORT = "EVAL_EXPORT"
SUBJECT_TYPE_PROPOSAL = "PROPOSAL"
SUBJECT_TYPE_EVALUATION = "EVALUATION"

REASON_NOT_REVIEWER = "NOT_REVIEWER"
REASON_NOT_ADMIN = "NOT_ADMIN"

REVIEW_STATES = ("PENDING", "EDITED", "ACCEPTED", "REJECTED")
TERMINAL_REVIEW_STATES = ("ACCEPTED", "REJECTED")

_MISSING: object = object()


class ReviewError(Exception):
    """Base class for review-service failures."""


class ProposalNotFoundError(ReviewError):
    """The requested proposal id does not exist."""


class ReviewAuthorizationError(AuthorizationError, ReviewError):
    """The caller is not the reviewer and does not hold Admin override."""


class AcceptanceBlockedError(ReviewError):
    """An FR-REV-06 / A3 / A4 / A6 / A13 gate blocked acceptance."""

    def __init__(self, code: str, details: tuple[str, ...] = ()) -> None:
        self.code = code
        self.details = details
        super().__init__(code + (f": {', '.join(details)}" if details else ""))


class StaleRevisionError(ReviewError):
    """``expected_revision_number`` does not match the latest revision (A9)."""

    def __init__(
        self,
        expected: int | None,
        current_revision_number: int,
        latest: EffectiveDraft | None,
    ) -> None:
        self.expected = expected
        self.current_revision_number = current_revision_number
        self.latest = latest
        super().__init__(
            "stale revision: expected"
            f" {expected!r}, current {current_revision_number}"
        )


class TerminalStateError(ReviewError):
    """The proposal is ACCEPTED or REJECTED and cannot change again."""

    def __init__(self, review_state: str) -> None:
        self.review_state = review_state
        super().__init__(f"proposal is terminal: {review_state}")


class NoChangesError(ReviewError):
    """An edit that would change nothing is rejected (A10)."""

    def __init__(self, summary: str) -> None:
        self.summary = summary
        super().__init__(f"edit changes nothing: {summary}")


class InvalidEditError(ReviewError):
    """The supplied edit is not representable in the editable vocabulary."""


class _ConfigLike(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True)
class ReviewPolicy:
    """Admin token configuration (same convention as Phase 5's G1)."""

    admin_role_tokens: tuple[str, ...] = DEFAULT_ADMIN_ROLE_TOKENS


def load_review_policy(config: _ConfigLike | None = None) -> ReviewPolicy:
    """Read ``ADMIN_ROLES`` (comma-separated, trimmed) or use the default."""
    if config is None:
        return ReviewPolicy()
    raw = config.get(ADMIN_ROLES_CONFIG_KEY)
    if raw is None or raw.strip() == "":
        return ReviewPolicy()
    tokens = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not tokens:
        raise ValueError(f"{ADMIN_ROLES_CONFIG_KEY} contains no usable role tokens")
    return ReviewPolicy(admin_role_tokens=tokens)


@dataclass(frozen=True)
class AssigneeSelection:
    """One assignee supplied by a reviewer.

    ``person_id`` set means a direct roster selection (MANUAL route). Without
    it the text is name-matched against active members of the source vertical
    (OD-REV-2: the Phase 5 DC-6 scope is unchanged).
    """

    text: str
    person_id: int | None = None


@dataclass(frozen=True)
class ProposalEdit:
    """An edit request; ``_MISSING`` means "keep the current value".

    Collections accept ``None`` (clear) or a complete replacement set (A2);
    for scalar nullable values ``None`` means clear. A blank description is
    normalized to NULL (A10).
    """

    description: object = _MISSING
    assignment_mode: object = _MISSING
    assignees: object = _MISSING
    people_needed: object = _MISSING
    people_needed_unlimited: object = _MISSING
    task_type: object = _MISSING
    responsible_vertical_id: object = _MISSING
    affected_vertical_ids: object = _MISSING
    attributes: object = _MISSING


@dataclass(frozen=True)
class EffectiveAssignee:
    assignee_text: str
    resolution: str
    person_id: int | None
    resolved_via: str | None


@dataclass(frozen=True)
class EffectiveDraft:
    description: str | None
    assignment_mode: str
    assignees: tuple[EffectiveAssignee, ...]
    people_needed: int | None
    people_needed_unlimited: int
    task_type: str
    responsible_vertical_id: int | None
    affected_vertical_ids: tuple[int, ...]
    attributes: tuple[tuple[str, str | None], ...]
    revision_number: int


@dataclass(frozen=True)
class ReviewMessage:
    message_id: int
    sender_person_id: int | None
    sender_name: str | None
    event_timestamp: str | None
    content: str | None
    reply_reference: str | None
    is_primary: bool


@dataclass(frozen=True)
class FieldProvenance:
    field: str
    original: object
    effective: object
    edited: bool


@dataclass(frozen=True)
class ProposalReviewView:
    proposal_id: int
    ptype: str
    review_state: str
    reviewer_person_id: int | None
    reviewer_name: str | None
    source_vertical_id: int
    bucket_id: int
    group_id: int
    primary_message: ReviewMessage
    context_messages: tuple[ReviewMessage, ...]
    draft: EffectiveDraft
    field_provenance: tuple[FieldProvenance, ...]
    generated_at: str
    model_id: str
    model_version: str
    prompt_version: str


@dataclass(frozen=True)
class EditResult:
    proposal_id: int
    revision_number: int
    changed_fields: tuple[str, ...]
    draft: EffectiveDraft


@dataclass(frozen=True)
class AcceptResult:
    proposal_id: int
    ptype: str
    entity_id: int
    created: bool
    approval_state: str | None
    notification_count: int


@dataclass(frozen=True)
class RejectResult:
    proposal_id: int
    decision_id: int
    note: str | None
    created: bool


@dataclass(frozen=True)
class EvalRevision:
    revision_number: int
    person_id: int
    changed_fields: tuple[str, ...]
    changed_at: str


@dataclass(frozen=True)
class EvalFieldDelta:
    """One field whose final value differs from the generated value."""

    field: str
    generated: object
    final: object


@dataclass(frozen=True)
class EvalProposal:
    proposal_id: int
    ptype: str
    review_state: str
    decision: str | None
    decided_by: int | None
    decided_at: str | None
    note: str | None
    revisions: tuple[EvalRevision, ...]
    generated_final_delta: tuple[EvalFieldDelta, ...]


@dataclass(frozen=True)
class _AuthContext:
    proposal: sqlite3.Row
    actor: sqlite3.Row
    reviewer_person_id: int | None
    admin_override: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalized(value: object) -> str:
    return str(value).strip().casefold()


def _optional_note(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _admin_role_tokens(policy: ReviewPolicy) -> frozenset[str]:
    return frozenset(_normalized(token) for token in policy.admin_role_tokens)


def _proposal_row(connection: sqlite3.Connection, proposal_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT p.*, b.group_id, m.sender_person_id AS reviewer_person_id"
        " FROM proposal AS p"
        " JOIN bucket AS b ON b.id = p.bucket_id"
        " JOIN source_message AS m ON m.id = p.primary_message_id"
        " WHERE p.id = ?",
        (proposal_id,),
    ).fetchone()


def _person_row(connection: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, name, role, active FROM person WHERE id = ?", (person_id,)
    ).fetchone()


def _require_active_actor(
    connection: sqlite3.Connection,
    actor_person_id: int,
    *,
    operation: str,
    subject_type: str,
    subject_id: int | None,
) -> sqlite3.Row:
    """Phase 6 adapter over the shared active-actor check (plan §4.4).

    The shared helper raises :class:`backend.authorization.AuthorizationError`;
    review callers keep catching ``ReviewAuthorizationError`` with the same
    reason string.
    """
    try:
        return require_active_actor(
            connection,
            actor_person_id,
            operation=operation,
            subject_type=subject_type,
            subject_id=subject_id,
        )
    except AuthorizationError as exc:
        raise ReviewAuthorizationError(exc.reason) from exc


def _authorize(
    connection: sqlite3.Connection,
    proposal_id: int,
    actor_person_id: int,
    *,
    operation: str,
    policy: ReviewPolicy,
) -> _AuthContext:
    proposal = _proposal_row(connection, proposal_id)
    if proposal is None:
        raise ProposalNotFoundError(f"proposal {proposal_id} not found")

    actor = _require_active_actor(
        connection,
        actor_person_id,
        operation=operation,
        subject_type=SUBJECT_TYPE_PROPOSAL,
        subject_id=proposal_id,
    )

    reviewer_person_id = proposal["reviewer_person_id"]
    reviewer = None if reviewer_person_id is None else int(reviewer_person_id)
    admin_override = _normalized(actor["role"]) in _admin_role_tokens(policy)
    if not admin_override and reviewer != actor_person_id:
        _record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=operation,
            subject_type=SUBJECT_TYPE_PROPOSAL,
            subject_id=proposal_id,
            reason=REASON_NOT_REVIEWER,
        )
        raise ReviewAuthorizationError(REASON_NOT_REVIEWER)
    return _AuthContext(
        proposal=proposal,
        actor=actor,
        reviewer_person_id=reviewer,
        admin_override=admin_override,
    )


def _authorize_admin(
    connection: sqlite3.Connection,
    actor_person_id: int,
    *,
    operation: str,
    policy: ReviewPolicy,
) -> sqlite3.Row:
    """Admin-only authorization for internal surfaces (FR-AUT-01/04).

    Uses the same active-actor checks and the same configurable Admin token
    as proposal review; a denial is recorded before the error is raised.
    """
    actor = _require_active_actor(
        connection,
        actor_person_id,
        operation=operation,
        subject_type=SUBJECT_TYPE_EVALUATION,
        subject_id=None,
    )
    if _normalized(actor["role"]) not in _admin_role_tokens(policy):
        _record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=operation,
            subject_type=SUBJECT_TYPE_EVALUATION,
            subject_id=None,
            reason=REASON_NOT_ADMIN,
        )
        raise ReviewAuthorizationError(REASON_NOT_ADMIN)
    return actor


# ---------------------------------------------------------------------------
# Effective draft / revisions
# ---------------------------------------------------------------------------


def _assignee_rows_from_proposal(
    connection: sqlite3.Connection, proposal_id: int
) -> tuple[EffectiveAssignee, ...]:
    rows = connection.execute(
        "SELECT assignee_text, resolution, person_id, resolved_via"
        " FROM proposal_assignee WHERE proposal_id = ? ORDER BY id",
        (proposal_id,),
    ).fetchall()
    return tuple(
        EffectiveAssignee(
            assignee_text=str(row["assignee_text"]),
            resolution=str(row["resolution"]),
            person_id=None if row["person_id"] is None else int(row["person_id"]),
            resolved_via=None if row["resolved_via"] is None else str(row["resolved_via"]),
        )
        for row in rows
    )


def _assignee_rows_from_revision(
    connection: sqlite3.Connection, revision_id: int
) -> tuple[EffectiveAssignee, ...]:
    rows = connection.execute(
        "SELECT assignee_text, resolution, person_id, resolved_via"
        " FROM revision_assignee WHERE revision_id = ? ORDER BY id",
        (revision_id,),
    ).fetchall()
    return tuple(
        EffectiveAssignee(
            assignee_text=str(row["assignee_text"]),
            resolution=str(row["resolution"]),
            person_id=None if row["person_id"] is None else int(row["person_id"]),
            resolved_via=None if row["resolved_via"] is None else str(row["resolved_via"]),
        )
        for row in rows
    )


def _latest_revision(
    connection: sqlite3.Connection, proposal_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM proposal_revision WHERE proposal_id = ?"
        " ORDER BY revision_number DESC LIMIT 1",
        (proposal_id,),
    ).fetchone()


def _proposal_attributes(
    connection: sqlite3.Connection, proposal_id: int
) -> tuple[tuple[str, str | None], ...]:
    rows = connection.execute(
        "SELECT attr_key, attr_value FROM proposal_attribute"
        " WHERE proposal_id = ? ORDER BY attr_key",
        (proposal_id,),
    ).fetchall()
    return tuple(
        (str(row["attr_key"]), None if row["attr_value"] is None else str(row["attr_value"]))
        for row in rows
    )


def _revision_attributes(
    connection: sqlite3.Connection, revision_id: int
) -> tuple[tuple[str, str | None], ...]:
    rows = connection.execute(
        "SELECT attr_key, attr_value FROM revision_attribute"
        " WHERE revision_id = ? ORDER BY attr_key",
        (revision_id,),
    ).fetchall()
    return tuple(
        (str(row["attr_key"]), None if row["attr_value"] is None else str(row["attr_value"]))
        for row in rows
    )


def _proposal_affected_verticals(
    connection: sqlite3.Connection, proposal_id: int
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT vertical_id FROM proposal_affected_vertical"
        " WHERE proposal_id = ? ORDER BY vertical_id",
        (proposal_id,),
    ).fetchall()
    return tuple(int(row["vertical_id"]) for row in rows)


def _revision_affected_verticals(
    connection: sqlite3.Connection, revision_id: int
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT vertical_id FROM revision_affected_vertical"
        " WHERE revision_id = ? ORDER BY vertical_id",
        (revision_id,),
    ).fetchall()
    return tuple(int(row["vertical_id"]) for row in rows)


def _original_draft(connection: sqlite3.Connection, proposal: sqlite3.Row) -> EffectiveDraft:
    proposal_id = int(proposal["id"])
    return EffectiveDraft(
        description=None if proposal["description"] is None else str(proposal["description"]),
        assignment_mode=str(proposal["assignment_mode"]),
        assignees=_assignee_rows_from_proposal(connection, proposal_id),
        people_needed=(
            None if proposal["people_needed"] is None else int(proposal["people_needed"])
        ),
        people_needed_unlimited=int(proposal["people_needed_unlimited"]),
        task_type=str(proposal["task_type"]),
        responsible_vertical_id=(
            None
            if proposal["suggested_responsible_vertical_id"] is None
            else int(proposal["suggested_responsible_vertical_id"])
        ),
        affected_vertical_ids=_proposal_affected_verticals(connection, proposal_id),
        attributes=_proposal_attributes(connection, proposal_id),
        revision_number=0,
    )


def _effective_draft(connection: sqlite3.Connection, proposal: sqlite3.Row) -> EffectiveDraft:
    revision = _latest_revision(connection, int(proposal["id"]))
    if revision is None:
        return _original_draft(connection, proposal)
    revision_id = int(revision["id"])
    return EffectiveDraft(
        description=None if revision["description"] is None else str(revision["description"]),
        assignment_mode=str(revision["assignment_mode"]),
        assignees=_assignee_rows_from_revision(connection, revision_id),
        people_needed=(
            None if revision["people_needed"] is None else int(revision["people_needed"])
        ),
        people_needed_unlimited=int(revision["people_needed_unlimited"]),
        task_type=str(revision["task_type"]),
        responsible_vertical_id=(
            None
            if revision["suggested_responsible_vertical_id"] is None
            else int(revision["suggested_responsible_vertical_id"])
        ),
        affected_vertical_ids=_revision_affected_verticals(connection, revision_id),
        attributes=_revision_attributes(connection, revision_id),
        revision_number=int(revision["revision_number"]),
    )


def _draft_signature(draft: EffectiveDraft) -> tuple[object, ...]:
    return (
        draft.description,
        draft.assignment_mode,
        tuple(
            (row.assignee_text, row.resolution, row.person_id, row.resolved_via)
            for row in draft.assignees
        ),
        draft.people_needed,
        draft.people_needed_unlimited,
        draft.task_type,
        draft.responsible_vertical_id,
        draft.affected_vertical_ids,
        draft.attributes,
    )


def _changed_fields(before: EffectiveDraft, after: EffectiveDraft) -> tuple[str, ...]:
    pairs = (
        ("description", before.description, after.description),
        ("assignment_mode", before.assignment_mode, after.assignment_mode),
        ("assignees", before.assignees, after.assignees),
        ("people_needed", before.people_needed, after.people_needed),
        (
            "people_needed_unlimited",
            before.people_needed_unlimited,
            after.people_needed_unlimited,
        ),
        ("task_type", before.task_type, after.task_type),
        (
            "responsible_vertical_id",
            before.responsible_vertical_id,
            after.responsible_vertical_id,
        ),
        ("affected_vertical_ids", before.affected_vertical_ids, after.affected_vertical_ids),
        ("attributes", before.attributes, after.attributes),
    )
    return tuple(name for name, old, new in pairs if old != new)


def _check_expected_revision(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
    expected_revision_number: int | None,
) -> EffectiveDraft:
    draft = _effective_draft(connection, proposal)
    current = draft.revision_number
    if expected_revision_number is None:
        expected = 0
    else:
        expected = expected_revision_number
    if expected != current:
        raise StaleRevisionError(
            expected_revision_number,
            current,
            draft if current else None,
        )
    return draft


# ---------------------------------------------------------------------------
# Assignee handling (A2/A6/A14)
# ---------------------------------------------------------------------------


def _person_is_in_vertical(
    connection: sqlite3.Connection, person_id: int, vertical_id: int
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM person_vertical WHERE person_id = ? AND vertical_id = ?",
        (person_id, vertical_id),
    ).fetchone()
    return row is not None


def _resolve_assignee_text(
    connection: sqlite3.Connection, text: str, source_vertical_id: int
) -> EffectiveAssignee:
    stripped = text.strip()
    if not stripped:
        raise InvalidEditError("assignee text must not be blank")
    rows = connection.execute(
        "SELECT p.id, p.name FROM person AS p"
        " JOIN person_vertical AS pv ON pv.person_id = p.id"
        " WHERE pv.vertical_id = ? AND p.active = 1"
        " AND lower(trim(p.name)) = lower(?)"
        " ORDER BY p.id",
        (source_vertical_id, stripped),
    ).fetchall()
    if len(rows) == 1:
        return EffectiveAssignee(
            assignee_text=stripped,
            resolution="RESOLVED",
            person_id=int(rows[0]["id"]),
            resolved_via="NAME_MATCH",
        )
    if len(rows) > 1:
        return EffectiveAssignee(
            assignee_text=stripped,
            resolution="UNRESOLVED",
            person_id=None,
            resolved_via=None,
        )
    return EffectiveAssignee(
        assignee_text=stripped,
        resolution="FLAGGED",
        person_id=None,
        resolved_via=None,
    )


def _manual_assignee(
    connection: sqlite3.Connection, selection: AssigneeSelection
) -> EffectiveAssignee:
    assert selection.person_id is not None
    person = _person_row(connection, selection.person_id)
    if person is None:
        raise InvalidEditError(f"unknown roster person {selection.person_id}")
    text = selection.text.strip()
    if not text:
        text = str(person["name"])
    return EffectiveAssignee(
        assignee_text=text,
        resolution="RESOLVED",
        person_id=selection.person_id,
        resolved_via="MANUAL",
    )


def _collapse_assignees(
    assignees: tuple[EffectiveAssignee, ...],
) -> tuple[EffectiveAssignee, ...]:
    """A14: one row per resolved person, first occurrence wins."""
    seen: set[int] = set()
    collapsed: list[EffectiveAssignee] = []
    for assignee in assignees:
        if assignee.person_id is not None:
            if assignee.person_id in seen:
                continue
            seen.add(assignee.person_id)
        collapsed.append(assignee)
    return tuple(collapsed)


def _prepare_assignees(
    connection: sqlite3.Connection,
    selections: tuple[AssigneeSelection, ...],
    source_vertical_id: int,
) -> tuple[EffectiveAssignee, ...]:
    prepared: list[EffectiveAssignee] = []
    for selection in selections:
        if selection.person_id is not None:
            prepared.append(_manual_assignee(connection, selection))
        else:
            prepared.append(
                _resolve_assignee_text(connection, selection.text, source_vertical_id)
            )
    return _collapse_assignees(tuple(prepared))


def _sender_role(connection: sqlite3.Connection, sender_person_id: int | None) -> str:
    """A13 fail-closed sender classification: exactly AH or Head."""
    if sender_person_id is None:
        raise AcceptanceBlockedError("SENDER_ROLE_UNSUPPORTED", ("sender is missing",))
    person = _person_row(connection, sender_person_id)
    if person is None:
        raise AcceptanceBlockedError("SENDER_ROLE_UNSUPPORTED", ("sender is missing",))
    token = _normalized(person["role"])
    if token == "ah":
        return "AH"
    if token == "head":
        return "Head"
    raise AcceptanceBlockedError(
        "SENDER_ROLE_UNSUPPORTED", (f"sender role {person['role']!r}",)
    )


def _validate_eligibility(
    connection: sqlite3.Connection,
    draft: EffectiveDraft,
    *,
    ptype: str,
    sender_role: str,
    source_vertical_id: int,
) -> None:
    """A6: active assignee, role pool and vertical pool."""
    if ptype != "TASK":
        return
    pool_vertical = draft.responsible_vertical_id or source_vertical_id
    allowed_roles = {"volunteer"} if sender_role == "AH" else {"volunteer", "ah"}
    for assignee in draft.assignees:
        if assignee.person_id is None:
            continue
        person = _person_row(connection, assignee.person_id)
        if person is None or int(person["active"]) != 1:
            raise AcceptanceBlockedError(
                "ASSIGNEE_NOT_ELIGIBLE",
                (f"person {assignee.person_id} is not an active roster person",),
            )
        if _normalized(person["role"]) not in allowed_roles:
            raise AcceptanceBlockedError(
                "ASSIGNEE_NOT_ELIGIBLE",
                (f"person {assignee.person_id} role is not in the {sender_role} pool",),
            )
        if not _person_is_in_vertical(connection, assignee.person_id, pool_vertical):
            raise AcceptanceBlockedError(
                "ASSIGNEE_NOT_ELIGIBLE",
                (f"person {assignee.person_id} is not in vertical {pool_vertical}",),
            )


# ---------------------------------------------------------------------------
# Acceptance gates (FR-REV-06, A3/A4/A6/A13)
# ---------------------------------------------------------------------------


def _validate_acceptance_gates(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
    draft: EffectiveDraft,
    sender_role: str,
) -> None:
    ptype = str(proposal["ptype"])
    source_vertical_id = int(proposal["source_vertical_id"])

    # Gate 1: unresolved / flagged assignees + mode/assignee contradictions.
    if ptype == "ANNOUNCEMENT" and draft.assignees:
        raise AcceptanceBlockedError(
            "MODE_ASSIGNEE_CONTRADICTION",
            ("an announcement cannot carry assignees",),
        )
    if draft.assignment_mode == "EXPLICIT":
        if any(row.resolution != "RESOLVED" for row in draft.assignees):
            raise AcceptanceBlockedError(
                "UNRESOLVED_ASSIGNEES",
                tuple(
                    f"{row.assignee_text}:{row.resolution}"
                    for row in draft.assignees
                    if row.resolution != "RESOLVED"
                ),
            )
    elif draft.assignees:
        raise AcceptanceBlockedError(
            "MODE_ASSIGNEE_CONTRADICTION",
            (f"{draft.assignment_mode} mode carries assignees",),
        )

    # Gate 2: required fields (FR-REV-06).
    if draft.description is None or not draft.description.strip():
        raise AcceptanceBlockedError("REQUIRED_FIELD_UNFILLED", ("description",))
    if ptype == "TASK":
        if draft.assignment_mode == "OPEN" and not (
            (draft.people_needed is not None and draft.people_needed > 0)
            or draft.people_needed_unlimited == 1
        ):
            raise AcceptanceBlockedError("REQUIRED_FIELD_UNFILLED", ("people_needed",))
        if draft.assignment_mode == "EXPLICIT" and not draft.assignees:
            raise AcceptanceBlockedError("REQUIRED_FIELD_UNFILLED", ("assignees",))
        if draft.task_type == "CROSS_VERTICAL" and draft.responsible_vertical_id is None:
            raise AcceptanceBlockedError(
                "REQUIRED_FIELD_UNFILLED", ("responsible_vertical_id",)
            )

    # Gate 3: task-type / vertical / mode pre-insert invariants (A3).
    if ptype == "TASK":
        if draft.assignment_mode == "NONE":
            raise AcceptanceBlockedError("TASK_MODE_CONTRADICTION")
        responsible = draft.responsible_vertical_id
        if draft.task_type == "CROSS_VERTICAL":
            if responsible == source_vertical_id:
                raise AcceptanceBlockedError("CROSS_VERTICAL_VERTICAL_CONTRADICTION")
        else:
            if responsible is not None and responsible != source_vertical_id:
                raise AcceptanceBlockedError("INTERNAL_VERTICAL_CONTRADICTION")

    # Gate 4: assignee eligibility (A6).
    _validate_eligibility(
        connection,
        draft,
        ptype=ptype,
        sender_role=sender_role,
        source_vertical_id=source_vertical_id,
    )


def _people_needed_for_task(draft: EffectiveDraft) -> tuple[int | None, int]:
    """A11: the single place holding the task PEOPLE-NEEDED encoding.

    PB-3 (0017) stores ``people_needed NULL`` for EXPLICIT tasks and keeps the
    positive-count / unlimited encoding for OPEN tasks.
    """
    if draft.assignment_mode == "EXPLICIT":
        return None, 0
    if draft.people_needed_unlimited == 1:
        return None, 1
    return draft.people_needed, 0


def _approval_recipient_ids(
    connection: sqlite3.Connection,
    vertical_id: int,
    *,
    exclude_person_id: int | None,
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT DISTINCT p.id FROM person AS p"
        " JOIN person_vertical AS pv ON pv.person_id = p.id"
        " WHERE pv.vertical_id = ? AND p.active = 1"
        " AND lower(trim(p.role)) IN ('ah', 'head')"
        " ORDER BY p.id",
        (vertical_id,),
    ).fetchall()
    return tuple(
        int(row["id"])
        for row in rows
        if exclude_person_id is None or int(row["id"]) != exclude_person_id
    )


def _active_role_ids(
    connection: sqlite3.Connection, vertical_id: int, role_token: str
) -> tuple[int, ...]:
    rows = connection.execute(
        "SELECT DISTINCT p.id FROM person AS p"
        " JOIN person_vertical AS pv ON pv.person_id = p.id"
        " WHERE pv.vertical_id = ? AND p.active = 1"
        " AND lower(trim(p.role)) = ?"
        " ORDER BY p.id",
        (vertical_id, role_token.casefold()),
    ).fetchall()
    return tuple(int(row["id"]) for row in rows)


# ---------------------------------------------------------------------------
# Notification staging (rows only; Phase 9 owns delivery)
# ---------------------------------------------------------------------------


def _insert_proposal_ready(
    connection: sqlite3.Connection, proposal_id: int, recipient_person_id: int, created_at: str
) -> bool:
    return _insert_notification(
        connection,
        event_type=EVENT_PROPOSAL_READY,
        recipient_person_id=recipient_person_id,
        dedupe_key=proposal_ready_key(proposal_id),
        proposal_id=proposal_id,
        created_at=created_at,
    )


def notify_proposal_ready(connection: sqlite3.Connection, proposal_id: int) -> int | None:
    """Stage the reviewer notification for one **valid** proposal (A7/A18).

    Idempotent (FR-NOT-05): returns the proposal id only when a row was
    inserted, ``None`` when the proposal is missing, structurally invalid
    (A18), has no reviewer, or was already notified.
    """
    row = connection.execute(
        "SELECT p.id, p.structurally_valid, m.sender_person_id"
        " FROM proposal AS p"
        " JOIN source_message AS m ON m.id = p.primary_message_id"
        " WHERE p.id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None or int(row["structurally_valid"]) != 1:
        return None
    sender = row["sender_person_id"]
    if sender is None:
        return None
    inserted = _insert_proposal_ready(
        connection, proposal_id, int(sender), _utc_now()
    )
    return proposal_id if inserted else None


def sweep_unnotified_proposals(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Idempotent recovery sweep for valid proposals missing PROPOSAL_READY."""
    inserted: list[int] = []
    with transaction(connection):
        rows = connection.execute(
            "SELECT p.id, m.sender_person_id FROM proposal AS p"
            " JOIN source_message AS m ON m.id = p.primary_message_id"
            " WHERE p.structurally_valid = 1 AND m.sender_person_id IS NOT NULL"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM notification AS n"
            "   WHERE n.dedupe_key = 'PROPOSAL_READY:' || p.id"
            " ) ORDER BY p.id"
        ).fetchall()
        created_at = _utc_now()
        for row in rows:
            if _insert_proposal_ready(
                connection, int(row["id"]), int(row["sender_person_id"]), created_at
            ):
                inserted.append(int(row["id"]))
    return tuple(inserted)


# ---------------------------------------------------------------------------
# Presentation view (FR-REV-02)
# ---------------------------------------------------------------------------


def _review_message(
    row: sqlite3.Row, *, primary_message_id: int
) -> ReviewMessage:
    return ReviewMessage(
        message_id=int(row["id"]),
        sender_person_id=(
            None if row["sender_person_id"] is None else int(row["sender_person_id"])
        ),
        sender_name=None if row["sender_name"] is None else str(row["sender_name"]),
        event_timestamp=(
            None if row["event_timestamp"] is None else str(row["event_timestamp"])
        ),
        content=None if row["content"] is None else str(row["content"]),
        reply_reference=(
            None if row["reply_reference"] is None else str(row["reply_reference"])
        ),
        is_primary=int(row["id"]) == primary_message_id,
    )


def _context_messages(
    connection: sqlite3.Connection, bucket_id: int, primary_message_id: int
) -> tuple[ReviewMessage, ...]:
    rows = connection.execute(
        "SELECT m.id, m.sender_person_id, m.event_timestamp, m.content,"
        " m.reply_reference, p.name AS sender_name"
        " FROM bucket_message AS bm"
        " JOIN source_message AS m ON m.id = bm.message_id"
        " LEFT JOIN person AS p ON p.id = m.sender_person_id"
        " WHERE bm.bucket_id = ? ORDER BY m.id",
        (bucket_id,),
    ).fetchall()
    return tuple(_review_message(row, primary_message_id=primary_message_id) for row in rows)


def _field_provenance(
    original: EffectiveDraft, effective: EffectiveDraft
) -> tuple[FieldProvenance, ...]:
    values = (
        ("description", original.description, effective.description),
        ("assignment_mode", original.assignment_mode, effective.assignment_mode),
        ("assignees", original.assignees, effective.assignees),
        ("people_needed", original.people_needed, effective.people_needed),
        (
            "people_needed_unlimited",
            original.people_needed_unlimited,
            effective.people_needed_unlimited,
        ),
        ("task_type", original.task_type, effective.task_type),
        (
            "responsible_vertical_id",
            original.responsible_vertical_id,
            effective.responsible_vertical_id,
        ),
        (
            "affected_vertical_ids",
            original.affected_vertical_ids,
            effective.affected_vertical_ids,
        ),
        ("attributes", original.attributes, effective.attributes),
    )
    return tuple(
        FieldProvenance(
            field=name,
            original=original_value,
            effective=effective_value,
            edited=original_value != effective_value,
        )
        for name, original_value, effective_value in values
    )


def get_proposal_review(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    actor_person_id: int,
    policy: ReviewPolicy | None = None,
    config: _ConfigLike | None = None,
) -> ProposalReviewView:
    """Return the reviewer presentation view (FR-REV-02). Read-only."""
    policy = policy or load_review_policy(config)
    auth = _authorize(
        connection,
        proposal_id,
        actor_person_id,
        operation=OPERATION_PROPOSAL_REVIEW,
        policy=policy,
    )
    proposal = auth.proposal
    primary_message_id = int(proposal["primary_message_id"])
    context = _context_messages(connection, int(proposal["bucket_id"]), primary_message_id)
    primary = next(
        (message for message in context if message.message_id == primary_message_id),
        None,
    )
    if primary is None:
        raise ReviewError("primary source message is missing from its bucket")
    original = _original_draft(connection, proposal)
    effective = _effective_draft(connection, proposal)
    reviewer = (
        None
        if auth.reviewer_person_id is None
        else _person_row(connection, auth.reviewer_person_id)
    )
    return ProposalReviewView(
        proposal_id=proposal_id,
        ptype=str(proposal["ptype"]),
        review_state=str(proposal["review_state"]),
        reviewer_person_id=auth.reviewer_person_id,
        reviewer_name=None if reviewer is None else str(reviewer["name"]),
        source_vertical_id=int(proposal["source_vertical_id"]),
        bucket_id=int(proposal["bucket_id"]),
        group_id=int(proposal["group_id"]),
        primary_message=primary,
        context_messages=context,
        draft=effective,
        field_provenance=_field_provenance(original, effective),
        generated_at=str(proposal["generated_at"]),
        model_id=str(proposal["model_id"]),
        model_version=str(proposal["model_version"]),
        prompt_version=str(proposal["prompt_version"]),
    )


# ---------------------------------------------------------------------------
# Edit pipeline (FR-REV-03/04/09, A2/A6/A9/A10/A14)
# ---------------------------------------------------------------------------


def _supplied(edit: ProposalEdit, name: str) -> object:
    return getattr(edit, name)


def _validate_vertical_exists(connection: sqlite3.Connection, vertical_id: int) -> None:
    row = connection.execute("SELECT 1 FROM vertical WHERE id = ?", (vertical_id,)).fetchone()
    if row is None:
        raise InvalidEditError(f"unknown vertical {vertical_id}")


def _apply_edit(
    connection: sqlite3.Connection,
    proposal: sqlite3.Row,
    current: EffectiveDraft,
    edit: ProposalEdit,
) -> EffectiveDraft:
    source_vertical_id = int(proposal["source_vertical_id"])
    raw_description = _supplied(edit, "description")
    if raw_description is _MISSING:
        description = current.description
    elif raw_description is None:
        description = None
    else:
        text = str(raw_description).strip()
        description = text or None

    raw_mode = _supplied(edit, "assignment_mode")
    if raw_mode is _MISSING:
        assignment_mode = current.assignment_mode
    else:
        assignment_mode = str(raw_mode)
        if assignment_mode not in ("EXPLICIT", "OPEN", "NONE"):
            raise InvalidEditError(f"invalid assignment_mode {assignment_mode!r}")

    raw_assignees = _supplied(edit, "assignees")
    if raw_assignees is _MISSING:
        assignees = current.assignees
    elif raw_assignees is None:
        assignees = ()
    else:
        if not isinstance(raw_assignees, tuple):
            raise InvalidEditError("assignees must be a tuple of AssigneeSelection")
        for selection in raw_assignees:
            if not isinstance(selection, AssigneeSelection):
                raise InvalidEditError("assignees must contain AssigneeSelection values")
        assignees = _prepare_assignees(connection, raw_assignees, source_vertical_id)

    raw_people = _supplied(edit, "people_needed")
    if raw_people is _MISSING:
        people_needed = current.people_needed
    elif raw_people is None:
        people_needed = None
    else:
        if not isinstance(raw_people, int) or isinstance(raw_people, bool):
            raise InvalidEditError("people_needed must be an integer or None")
        if raw_people <= 0:
            raise InvalidEditError("people_needed must be positive or None")
        people_needed = raw_people

    raw_unlimited = _supplied(edit, "people_needed_unlimited")
    if raw_unlimited is _MISSING:
        people_needed_unlimited = current.people_needed_unlimited
    else:
        if raw_unlimited not in (0, 1) or isinstance(raw_unlimited, bool):
            raise InvalidEditError("people_needed_unlimited must be 0 or 1")
        people_needed_unlimited = int(str(raw_unlimited))
    if people_needed_unlimited == 1 and people_needed is not None:
        raise InvalidEditError("people_needed and UNLIMITED cannot both be set")

    raw_task_type = _supplied(edit, "task_type")
    if raw_task_type is _MISSING:
        task_type = current.task_type
    else:
        task_type = str(raw_task_type)
        if task_type not in ("INTERNAL", "CROSS_VERTICAL"):
            raise InvalidEditError(f"invalid task_type {task_type!r}")

    raw_responsible = _supplied(edit, "responsible_vertical_id")
    if raw_responsible is _MISSING:
        responsible_vertical_id = current.responsible_vertical_id
    elif raw_responsible is None:
        responsible_vertical_id = None
    else:
        if not isinstance(raw_responsible, int) or isinstance(raw_responsible, bool):
            raise InvalidEditError("responsible_vertical_id must be an integer or None")
        _validate_vertical_exists(connection, raw_responsible)
        responsible_vertical_id = raw_responsible

    raw_affected = _supplied(edit, "affected_vertical_ids")
    if raw_affected is _MISSING:
        affected_vertical_ids = current.affected_vertical_ids
    elif raw_affected is None:
        affected_vertical_ids = ()
    else:
        if not isinstance(raw_affected, tuple):
            raise InvalidEditError("affected_vertical_ids must be a tuple of ids")
        values: list[int] = []
        for value in raw_affected:
            if not isinstance(value, int) or isinstance(value, bool):
                raise InvalidEditError("affected_vertical_ids must contain integers")
            _validate_vertical_exists(connection, value)
            values.append(value)
        affected_vertical_ids = tuple(sorted(set(values)))

    raw_attributes = _supplied(edit, "attributes")
    if raw_attributes is _MISSING:
        attributes = current.attributes
    elif raw_attributes is None:
        attributes = ()
    else:
        if not isinstance(raw_attributes, tuple):
            raise InvalidEditError("attributes must be a tuple of (key, value) pairs")
        collected: dict[str, str | None] = {}
        for entry in raw_attributes:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise InvalidEditError("attributes must be (key, value) pairs")
            key, value = entry
            if not isinstance(key, str) or not key.strip():
                raise InvalidEditError("attribute keys must be non-blank text")
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise InvalidEditError("attribute values must be non-blank text or None")
            if key in collected:
                raise InvalidEditError(f"duplicate attribute key {key!r}")
            collected[key] = value
        attributes = tuple(sorted(collected.items()))

    candidate = EffectiveDraft(
        description=description,
        assignment_mode=assignment_mode,
        assignees=assignees,
        people_needed=people_needed,
        people_needed_unlimited=people_needed_unlimited,
        task_type=task_type,
        responsible_vertical_id=responsible_vertical_id,
        affected_vertical_ids=affected_vertical_ids,
        attributes=attributes,
        revision_number=current.revision_number + 1,
    )
    if _draft_signature(candidate) == _draft_signature(current):
        raise NoChangesError(repr(_draft_signature(candidate)))
    if str(proposal["ptype"]) == "ANNOUNCEMENT" and candidate.assignees:
        raise AcceptanceBlockedError(
            "MODE_ASSIGNEE_CONTRADICTION",
            ("an announcement cannot carry assignees",),
        )
    if str(proposal["ptype"]) == "TASK":
        sender_role = _sender_role(connection, proposal["reviewer_person_id"])
        _validate_eligibility(
            connection,
            candidate,
            ptype="TASK",
            sender_role=sender_role,
            source_vertical_id=source_vertical_id,
        )
    return candidate


def save_proposal_edit(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    actor_person_id: int,
    expected_revision_number: int | None,
    changes: ProposalEdit,
    policy: ReviewPolicy | None = None,
    config: _ConfigLike | None = None,
) -> EditResult:
    """Append one immutable revision; never accepts and never edits proposal rows."""
    policy = policy or load_review_policy(config)
    _authorize(
        connection,
        proposal_id,
        actor_person_id,
        operation=OPERATION_PROPOSAL_REVIEW,
        policy=policy,
    )
    with transaction(connection):
        proposal = _proposal_row(connection, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(f"proposal {proposal_id} not found")
        state = str(proposal["review_state"])
        if state in TERMINAL_REVIEW_STATES:
            raise TerminalStateError(state)
        current = _check_expected_revision(
            connection, proposal, expected_revision_number
        )
        candidate = _apply_edit(connection, proposal, current, changes)
        changed_fields = _changed_fields(current, candidate)
        revision_number = current.revision_number + 1
        now = _utc_now()
        assignees_text = (
            ", ".join(row.assignee_text for row in candidate.assignees)
            or None
        )
        cursor = connection.execute(
            "INSERT INTO proposal_revision (proposal_id, revision_number,"
            " person_id, changed_fields, changed_at, description,"
            " assignment_mode, proposed_assignees_text, people_needed,"
            " people_needed_unlimited, task_type,"
            " suggested_responsible_vertical_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal_id,
                revision_number,
                actor_person_id,
                json.dumps(list(changed_fields), separators=(",", ":")),
                now,
                candidate.description,
                candidate.assignment_mode,
                assignees_text,
                candidate.people_needed,
                candidate.people_needed_unlimited,
                candidate.task_type,
                candidate.responsible_vertical_id,
            ),
        )
        revision_id = int(cursor.lastrowid or 0)
        for assignee in candidate.assignees:
            connection.execute(
                "INSERT INTO revision_assignee (revision_id, assignee_text,"
                " resolution, person_id, resolved_via)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    revision_id,
                    assignee.assignee_text,
                    assignee.resolution,
                    assignee.person_id,
                    assignee.resolved_via,
                ),
            )
        for key, value in candidate.attributes:
            connection.execute(
                "INSERT INTO revision_attribute (revision_id, attr_key, attr_value)"
                " VALUES (?, ?, ?)",
                (revision_id, key, value),
            )
        for vertical_id in candidate.affected_vertical_ids:
            connection.execute(
                "INSERT INTO revision_affected_vertical (revision_id, vertical_id)"
                " VALUES (?, ?)",
                (revision_id, vertical_id),
            )
        connection.execute(
            "UPDATE proposal SET review_state = 'EDITED' WHERE id = ?",
            (proposal_id,),
        )
    return EditResult(
        proposal_id=proposal_id,
        revision_number=revision_number,
        changed_fields=changed_fields,
        draft=EffectiveDraft(
            description=candidate.description,
            assignment_mode=candidate.assignment_mode,
            assignees=candidate.assignees,
            people_needed=candidate.people_needed,
            people_needed_unlimited=candidate.people_needed_unlimited,
            task_type=candidate.task_type,
            responsible_vertical_id=candidate.responsible_vertical_id,
            affected_vertical_ids=candidate.affected_vertical_ids,
            attributes=candidate.attributes,
            revision_number=revision_number,
        ),
    )


# ---------------------------------------------------------------------------
# Accept / reject (FR-REV-05/06/07/10, FR-XV, A3/A4/A5/A9/A11/A13/A14/A15/A16)
# ---------------------------------------------------------------------------


def _existing_entity(
    connection: sqlite3.Connection, proposal_id: int, ptype: str
) -> tuple[int, str | None] | None:
    if ptype == "TASK":
        row = connection.execute(
            "SELECT id, approval_state FROM task WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"]), str(row["approval_state"])
    row = connection.execute(
        "SELECT id FROM announcement WHERE proposal_id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    return int(row["id"]), None


def _actor_vertical_id(
    connection: sqlite3.Connection, actor_person_id: int, source_vertical_id: int
) -> int | None:
    if _person_is_in_vertical(connection, actor_person_id, source_vertical_id):
        return source_vertical_id
    return None


def _insert_decision(
    connection: sqlite3.Connection,
    *,
    proposal_id: int,
    decision: str,
    decided_by: int,
    decided_at: str,
    note: str | None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO proposal_decision (proposal_id, decision, decided_by,"
        " decided_at, note) VALUES (?, ?, ?, ?, ?)",
        (proposal_id, decision, decided_by, decided_at, note),
    )
    return int(cursor.lastrowid or 0)


def _materialize_task(
    connection: sqlite3.Connection,
    *,
    proposal: sqlite3.Row,
    draft: EffectiveDraft,
    sender_role: str,
    actor_person_id: int,
    now: str,
    request_message: str | None,
) -> tuple[int, str, int]:
    """Write the task and its children; return (task_id, approval_state, staged)."""
    proposal_id = int(proposal["id"])
    source_vertical_id = int(proposal["source_vertical_id"])
    reviewer_person_id = proposal["reviewer_person_id"]
    creator_id = (
        int(reviewer_person_id)
        if reviewer_person_id is not None
        else actor_person_id
    )
    responsible_vertical_id = (
        source_vertical_id
        if draft.task_type == "INTERNAL"
        else draft.responsible_vertical_id
    )
    cross_vertical = (
        draft.task_type == "CROSS_VERTICAL"
        and responsible_vertical_id is not None
        and responsible_vertical_id != source_vertical_id
    )
    approval_state = "NOT_REQUIRED"
    recipients: tuple[int, ...] = ()
    head_recipients: tuple[int, ...] = ()
    if cross_vertical:
        assert responsible_vertical_id is not None
        if sender_role == "AH":
            approval_state = "PENDING"
            recipients = _approval_recipient_ids(
                connection,
                responsible_vertical_id,
                exclude_person_id=(
                    None if reviewer_person_id is None else int(reviewer_person_id)
                ),
            )
            if not recipients:
                raise AcceptanceBlockedError("NO_APPROVAL_RECIPIENTS")
        else:
            approval_state = "NOT_REQUIRED"
            head_recipients = _active_role_ids(
                connection, responsible_vertical_id, "Head"
            )
    people_needed, unlimited = _people_needed_for_task(draft)
    cursor = connection.execute(
        "INSERT INTO task (proposal_id, task_type, source_vertical_id,"
        " responsible_vertical_id, creator_id, description, assignment_mode,"
        " people_needed, people_needed_unlimited, approval_state,"
        " cancellation, version, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, ?)",
        (
            proposal_id,
            draft.task_type,
            source_vertical_id,
            responsible_vertical_id,
            creator_id,
            draft.description,
            draft.assignment_mode,
            people_needed,
            unlimited,
            approval_state,
            now,
        ),
    )
    task_id = int(cursor.lastrowid or 0)
    assignees = _collapse_assignees(draft.assignees)
    for assignee in assignees:
        assert assignee.person_id is not None
        connection.execute(
            "INSERT INTO task_assignee (task_id, person_id, agreement,"
            " agreed_at, work_status, cancelled, added_at)"
            " VALUES (?, ?, 'AWAITING', NULL, 'NOT_STARTED', 0, ?)",
            (task_id, assignee.person_id, now),
        )
    actor = _person_row(connection, actor_person_id)
    assert actor is not None
    connection.execute(
        "INSERT INTO task_history (task_id, field_name, old_value, new_value,"
        " changed_at, actor_person_id, actor_role, actor_vertical_id, reason)"
        " VALUES (?, 'TASK_CREATED', NULL, 'ACTIVE', ?, ?, ?, ?,"
        " 'PROPOSAL_ACCEPTED')",
        (
            task_id,
            now,
            actor_person_id,
            str(actor["role"]),
            _actor_vertical_id(connection, actor_person_id, source_vertical_id),
        ),
    )
    for key, value in draft.attributes:
        connection.execute(
            "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
            " VALUES (?, ?, ?)",
            (task_id, key, value),
        )
    for vertical_id in draft.affected_vertical_ids:
        connection.execute(
            "INSERT INTO task_affected_vertical (task_id, vertical_id)"
            " VALUES (?, ?)",
            (task_id, vertical_id),
        )

    approval_request_id: int | None = None
    if cross_vertical and approval_state == "PENDING":
        assert responsible_vertical_id is not None
        cursor = connection.execute(
            "INSERT INTO approval_request (task_id, requested_by,"
            " request_message, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                reviewer_person_id,
                _optional_note(request_message),
                now,
            ),
        )
        approval_request_id = int(cursor.lastrowid or 0)
        for recipient in recipients:
            connection.execute(
                "INSERT INTO approval_recipient (request_id, person_id)"
                " VALUES (?, ?)",
                (approval_request_id, recipient),
            )
        request_actor_id = (
            int(reviewer_person_id)
            if reviewer_person_id is not None
            else actor_person_id
        )
        request_reason = _optional_note(request_message) or "CROSS_VERTICAL_REQUEST"
        connection.execute(
            "INSERT INTO task_history (task_id, field_name, old_value,"
            " new_value, changed_at, actor_person_id, actor_role,"
            " actor_vertical_id, reason)"
            " VALUES (?, 'APPROVAL_REQUEST', NULL, 'PENDING', ?, ?, ?, ?, ?)",
            (
                task_id,
                now,
                request_actor_id,
                sender_role,
                _actor_vertical_id(
                    connection, request_actor_id, source_vertical_id
                ),
                request_reason,
            ),
        )

    staged = 0
    hidden = approval_state == "PENDING"
    if not hidden:
        for assignee in assignees:
            assert assignee.person_id is not None
            staged += int(
                _insert_notification(
                    connection,
                    event_type=EVENT_TASK_ASSIGNED,
                    recipient_person_id=assignee.person_id,
                    dedupe_key=task_assignee_key(
                        EVENT_TASK_ASSIGNED, task_id, assignee.person_id
                    ),
                    task_id=task_id,
                    created_at=now,
                )
            )
    if draft.assignment_mode == "OPEN" and approval_state == "NOT_REQUIRED":
        pool_vertical = responsible_vertical_id or source_vertical_id
        for person_id in _active_role_ids(connection, pool_vertical, "Volunteer"):
            staged += int(
                _insert_notification(
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
    if approval_request_id is not None:
        for recipient in recipients:
            staged += int(
                _insert_notification(
                    connection,
                    event_type=EVENT_CROSS_VERTICAL_REQUEST,
                    recipient_person_id=recipient,
                    dedupe_key=approval_request_key(
                        approval_request_id, recipient
                    ),
                    approval_request_id=approval_request_id,
                    created_at=now,
                )
            )
    if cross_vertical and sender_role == "Head":
        for person_id in head_recipients:
            staged += int(
                _insert_notification(
                    connection,
                    event_type=EVENT_HEAD_CROSS_VERTICAL_TASK,
                    recipient_person_id=person_id,
                    dedupe_key=task_assignee_key(
                        EVENT_HEAD_CROSS_VERTICAL_TASK, task_id, person_id
                    ),
                    task_id=task_id,
                    created_at=now,
                )
            )
    return task_id, approval_state, staged


def _materialize_announcement(
    connection: sqlite3.Connection,
    *,
    proposal: sqlite3.Row,
    sender_role: str,
    actor_person_id: int,
    now: str,
) -> int:
    audience_type = "VERTICAL" if sender_role == "Head" else "AH_VOLUNTEERS"
    ah_person_id = (
        int(proposal["reviewer_person_id"])
        if audience_type == "AH_VOLUNTEERS" and proposal["reviewer_person_id"] is not None
        else None
    )
    cursor = connection.execute(
        "INSERT INTO announcement (proposal_id, accepted_by, accepted_at,"
        " audience_type, audience_vertical_id, audience_ah_person_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(proposal["id"]),
            actor_person_id,
            now,
            audience_type,
            int(proposal["source_vertical_id"]),
            ah_person_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def accept_proposal(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    actor_person_id: int,
    expected_revision_number: int | None,
    note: str | None = None,
    request_message: str | None = None,
    policy: ReviewPolicy | None = None,
    config: _ConfigLike | None = None,
) -> AcceptResult:
    """Accept one proposal in a single transaction (FR-REV-05, AC-18)."""
    policy = policy or load_review_policy(config)
    _authorize(
        connection,
        proposal_id,
        actor_person_id,
        operation=OPERATION_PROPOSAL_REVIEW,
        policy=policy,
    )
    with transaction(connection):
        proposal = _proposal_row(connection, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(f"proposal {proposal_id} not found")
        ptype = str(proposal["ptype"])
        state = str(proposal["review_state"])
        if state == "ACCEPTED":
            existing = _existing_entity(connection, proposal_id, ptype)
            if existing is None:
                raise ReviewError("accepted proposal has no materialized entity")
            entity_id, approval_state = existing
            return AcceptResult(
                proposal_id=proposal_id,
                ptype=ptype,
                entity_id=entity_id,
                created=False,
                approval_state=approval_state,
                notification_count=0,
            )
        if state == "REJECTED":
            raise TerminalStateError(state)
        draft = _check_expected_revision(connection, proposal, expected_revision_number)
        sender_role = _sender_role(connection, proposal["reviewer_person_id"])
        _validate_acceptance_gates(connection, proposal, draft, sender_role)
        now = _utc_now()
        decision_note = _optional_note(note)
        connection.execute(
            "UPDATE proposal SET review_state = 'ACCEPTED' WHERE id = ?",
            (proposal_id,),
        )
        _insert_decision(
            connection,
            proposal_id=proposal_id,
            decision="ACCEPTED",
            decided_by=actor_person_id,
            decided_at=now,
            note=decision_note,
        )
        if ptype == "TASK":
            entity_id, approval_state, staged = _materialize_task(
                connection,
                proposal=proposal,
                draft=draft,
                sender_role=sender_role,
                actor_person_id=actor_person_id,
                now=now,
                request_message=request_message,
            )
        else:
            entity_id = _materialize_announcement(
                connection,
                proposal=proposal,
                sender_role=sender_role,
                actor_person_id=actor_person_id,
                now=now,
            )
            approval_state = None
            staged = 0  # A16: no ANNOUNCEMENT notification rows in Phase 6.
    return AcceptResult(
        proposal_id=proposal_id,
        ptype=ptype,
        entity_id=entity_id,
        created=True,
        approval_state=approval_state,
        notification_count=staged,
    )


def reject_proposal(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    actor_person_id: int,
    note: str | None = None,
    policy: ReviewPolicy | None = None,
    config: _ConfigLike | None = None,
) -> RejectResult:
    """Reject one proposal, keeping the proposal and all revisions (FR-REV-07)."""
    policy = policy or load_review_policy(config)
    _authorize(
        connection,
        proposal_id,
        actor_person_id,
        operation=OPERATION_PROPOSAL_REVIEW,
        policy=policy,
    )
    with transaction(connection):
        proposal = _proposal_row(connection, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(f"proposal {proposal_id} not found")
        state = str(proposal["review_state"])
        if state in TERMINAL_REVIEW_STATES:
            raise TerminalStateError(state)
        note_value = _optional_note(note)
        now = _utc_now()
        decision_id = _insert_decision(
            connection,
            proposal_id=proposal_id,
            decision="REJECTED",
            decided_by=actor_person_id,
            decided_at=now,
            note=note_value,
        )
        connection.execute(
            "UPDATE proposal SET review_state = 'REJECTED' WHERE id = ?",
            (proposal_id,),
        )
    return RejectResult(
        proposal_id=proposal_id,
        decision_id=decision_id,
        note=note_value,
        created=True,
    )


# ---------------------------------------------------------------------------
# Evaluation export (FR-REV-08)
# ---------------------------------------------------------------------------


def list_reviewed_proposals_for_eval(
    connection: sqlite3.Connection,
    *,
    actor_person_id: int,
    policy: ReviewPolicy | None = None,
    config: _ConfigLike | None = None,
) -> tuple[EvalProposal, ...]:
    """Return labelled proposals with their full revision history (FR-REV-08).

    Admin-only internal read (FR-AUT-01/04): the caller must be an active
    person holding a configured Admin token; a denial is recorded before
    ``ReviewAuthorizationError`` is raised (FR-AUT-06, A17).

    A proposal is labelled evaluation data when it was edited (at least one
    revision, whatever the final state) or rejected (the rejection is the
    label, even without an edit). An untouched accepted or pending proposal
    carries no reviewer label and is not exported.
    """
    policy = policy or load_review_policy(config)
    _authorize_admin(
        connection,
        actor_person_id,
        operation=OPERATION_EVAL_EXPORT,
        policy=policy,
    )
    rows = connection.execute(
        "SELECT p.id FROM proposal AS p"
        " WHERE p.review_state = 'REJECTED'"
        "    OR EXISTS ("
        "        SELECT 1 FROM proposal_revision AS r WHERE r.proposal_id = p.id"
        "    )"
        " ORDER BY p.id"
    ).fetchall()
    result: list[EvalProposal] = []
    for row in rows:
        proposal_id = int(row["id"])
        proposal = _proposal_row(connection, proposal_id)
        if proposal is None:  # pragma: no cover - FK guarantees the row
            raise ReviewError(f"proposal {proposal_id} not found")
        decision = connection.execute(
            "SELECT decision, decided_by, decided_at, note"
            " FROM proposal_decision WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        revisions = connection.execute(
            "SELECT revision_number, person_id, changed_fields, changed_at"
            " FROM proposal_revision WHERE proposal_id = ?"
            " ORDER BY revision_number",
            (proposal_id,),
        ).fetchall()
        generated_final_delta = tuple(
            EvalFieldDelta(
                field=change.field,
                generated=change.original,
                final=change.effective,
            )
            for change in _field_provenance(
                _original_draft(connection, proposal),
                _effective_draft(connection, proposal),
            )
            if change.edited
        )
        result.append(
            EvalProposal(
                proposal_id=proposal_id,
                ptype=str(proposal["ptype"]),
                review_state=str(proposal["review_state"]),
                decision=None if decision is None else str(decision["decision"]),
                decided_by=(
                    None if decision is None else int(decision["decided_by"])
                ),
                decided_at=(
                    None if decision is None else str(decision["decided_at"])
                ),
                note=None if decision is None else decision["note"],
                revisions=tuple(
                    EvalRevision(
                        revision_number=int(revision["revision_number"]),
                        person_id=int(revision["person_id"]),
                        changed_fields=tuple(json.loads(str(revision["changed_fields"]))),
                        changed_at=str(revision["changed_at"]),
                    )
                    for revision in revisions
                ),
                generated_final_delta=generated_final_delta,
            )
        )
    return tuple(result)
