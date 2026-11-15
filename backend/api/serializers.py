"""Response payloads as explicit allow-lists (W2/W3; plan §4.2, NFR-SEC-03).

A serializer is the only place a field leaves the backend. It takes the
authenticated person the database returned, never a request value, and it never
includes a token except in the one verify response that issues it.

W3 adds the task and proposal-review payloads. Message content appears in
exactly one serializer (:func:`proposal_review_payload`), which is the Phase 6
reviewer view NFR-SEC-03 exception (b) allows; every other payload is built
from columns, never from source text. No payload includes a phone number.
"""

from __future__ import annotations

from backend.approvals import ApprovalView, DecisionResult
from backend.auth import AuthenticatedPerson, Session
from backend.notification_delivery import NotificationView
from backend.review import (
    AcceptResult,
    EditResult,
    EffectiveAssignee,
    EffectiveDraft,
    EvalProposal,
    FieldProvenance,
    ProposalReviewView,
    RejectResult,
    ReviewMessage,
)
from backend.tasks import TaskSummary


def person_payload(person: AuthenticatedPerson) -> dict[str, object]:
    """The caller's own identity: id, current role and current verticals."""
    return {
        "id": person.person_id,
        "role": person.role,
        "vertical_ids": list(person.vertical_ids),
    }


def session_payload(
    session: Session, person: AuthenticatedPerson
) -> dict[str, object]:
    """The one response that carries the plaintext token (returned once)."""
    return {
        "token": session.token,
        "expires_at": session.expires_at,
        "person": person_payload(person),
    }


def task_payload(task: TaskSummary) -> dict[str, object]:
    """One read-model row (plan §4.5). No content, no roster personal data."""
    return {
        "id": task.id,
        "task_type": task.task_type,
        "source_vertical_id": task.source_vertical_id,
        "responsible_vertical_id": task.responsible_vertical_id,
        "creator_id": task.creator_id,
        "description": task.description,
        "assignment_mode": task.assignment_mode,
        "people_needed": task.people_needed,
        "people_needed_unlimited": task.people_needed_unlimited,
        "cancellation": task.cancellation,
        "version": task.version,
        "accepted_count": task.accepted_count,
        "completed_count": task.completed_count,
    }


def _assignee_payload(assignee: EffectiveAssignee) -> dict[str, object]:
    return {
        "text": assignee.assignee_text,
        "resolution": assignee.resolution,
        "person_id": assignee.person_id,
        "resolved_via": assignee.resolved_via,
    }


def _draft_payload(draft: EffectiveDraft) -> dict[str, object]:
    return {
        "description": draft.description,
        "assignment_mode": draft.assignment_mode,
        "assignees": [_assignee_payload(item) for item in draft.assignees],
        "people_needed": draft.people_needed,
        "people_needed_unlimited": draft.people_needed_unlimited,
        "task_type": draft.task_type,
        "responsible_vertical_id": draft.responsible_vertical_id,
        "affected_vertical_ids": list(draft.affected_vertical_ids),
        "attributes": [
            {"key": key, "value": value} for key, value in draft.attributes
        ],
        "revision_number": draft.revision_number,
    }


def _message_payload(message: ReviewMessage) -> dict[str, object]:
    """The reviewer view of one source message (the one content exception)."""
    return {
        "message_id": message.message_id,
        "sender_person_id": message.sender_person_id,
        "sender_name": message.sender_name,
        "event_timestamp": message.event_timestamp,
        "content": message.content,
        "reply_reference": message.reply_reference,
        "is_primary": message.is_primary,
    }


def _provenance_value(value: object) -> object:
    """Render one provenance value with the same allow-lists as the draft."""
    if isinstance(value, EffectiveAssignee):
        return _assignee_payload(value)
    if isinstance(value, (tuple, list)):
        return [_provenance_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _provenance_value(item) for key, item in value.items()}
    return value


def _provenance_payload(field: FieldProvenance) -> dict[str, object]:
    return {
        "field": field.field,
        "original": _provenance_value(field.original),
        "effective": _provenance_value(field.effective),
        "edited": field.edited,
    }


def proposal_review_payload(view: ProposalReviewView) -> dict[str, object]:
    """The reviewer presentation view (FR-REV-02); content leaves only here."""
    return {
        "proposal_id": view.proposal_id,
        "ptype": view.ptype,
        "review_state": view.review_state,
        "reviewer_person_id": view.reviewer_person_id,
        "reviewer_name": view.reviewer_name,
        "source_vertical_id": view.source_vertical_id,
        "bucket_id": view.bucket_id,
        "group_id": view.group_id,
        "primary_message": _message_payload(view.primary_message),
        "context_messages": [
            _message_payload(message) for message in view.context_messages
        ],
        "draft": _draft_payload(view.draft),
        "field_provenance": [
            _provenance_payload(field) for field in view.field_provenance
        ],
        "generated_at": view.generated_at,
        "model_id": view.model_id,
        "model_version": view.model_version,
        "prompt_version": view.prompt_version,
    }


def edit_result_payload(result: EditResult) -> dict[str, object]:
    """One appended revision: what changed, never a whole proposal row."""
    return {
        "proposal_id": result.proposal_id,
        "revision_number": result.revision_number,
        "changed_fields": list(result.changed_fields),
        "draft": _draft_payload(result.draft),
    }


def accept_result_payload(result: AcceptResult) -> dict[str, object]:
    """The materialized entity handle and whether this call created it."""
    return {
        "proposal_id": result.proposal_id,
        "ptype": result.ptype,
        "entity_id": result.entity_id,
        "created": result.created,
        "approval_state": result.approval_state,
        "notification_count": result.notification_count,
    }


def reject_result_payload(result: RejectResult) -> dict[str, object]:
    """The stored rejection decision; the proposal itself is kept."""
    return {
        "proposal_id": result.proposal_id,
        "decision_id": result.decision_id,
        "note": result.note,
        "created": result.created,
    }


# ---------------------------------------------------------------------------
# W4: approvals, notifications, push endpoints and admin read models
# ---------------------------------------------------------------------------


def approval_payload(view: ApprovalView) -> dict[str, object]:
    """One approver-safe request projection (NFR-SEC-03(c)).

    Deliberately excludes source message content, bucket data and roster
    personal data; the description and the requester's note are the only text.
    """
    return {
        "request_id": view.request_id,
        "task_id": view.task_id,
        "requester_person_id": view.requester_person_id,
        "requester_name": view.requester_name,
        "request_message": view.request_message,
        "responsible_vertical_id": view.responsible_vertical_id,
        "responsible_vertical_name": view.responsible_vertical_name,
        "source_vertical_id": view.source_vertical_id,
        "source_vertical_name": view.source_vertical_name,
        "task_description": view.task_description,
        "task_type": view.task_type,
        "assignment_mode": view.assignment_mode,
        "people_needed": view.people_needed,
        "people_needed_unlimited": view.people_needed_unlimited,
        "created_at": view.created_at,
        "decision": view.decision,
        "decision_message": view.decision_message,
    }


def decision_payload(result: DecisionResult) -> dict[str, object]:
    """The observed decision outcome; ``changed`` says whether it was new."""
    return {
        "request_id": result.request_id,
        "task_id": result.task_id,
        "decision": result.decision,
        "message": result.message,
        "decided_by": result.decided_by,
        "decided_at": result.decided_at,
        "changed": result.changed,
        "notified": result.notified,
    }


def notification_payload(view: NotificationView) -> dict[str, object]:
    """One notification rendered from the current domain state (FR-NOT-03)."""
    return {
        "notification_id": view.notification_id,
        "event_type": view.event_type,
        "title": view.title,
        "body": view.body,
        "deep_link": view.deep_link,
        "created_at": view.created_at,
        "read_at": view.read_at,
    }


def push_endpoint_payload(endpoint_id: int, *, active: bool) -> dict[str, object]:
    """The endpoint handle only; the push token is never echoed."""
    return {"endpoint_id": endpoint_id, "active": active}


def eval_proposal_payload(item: EvalProposal) -> dict[str, object]:
    """One labelled proposal for the Admin evaluation export (FR-REV-08)."""
    return {
        "proposal_id": item.proposal_id,
        "ptype": item.ptype,
        "review_state": item.review_state,
        "decision": item.decision,
        "decided_by": item.decided_by,
        "decided_at": item.decided_at,
        "note": item.note,
        "revisions": [
            {
                "revision_number": revision.revision_number,
                "person_id": revision.person_id,
                "changed_fields": list(revision.changed_fields),
                "changed_at": revision.changed_at,
            }
            for revision in item.revisions
        ],
        "generated_final_delta": [
            {
                "field": delta.field,
                "generated": _provenance_value(delta.generated),
                "final": _provenance_value(delta.final),
            }
            for delta in item.generated_final_delta
        ],
    }
