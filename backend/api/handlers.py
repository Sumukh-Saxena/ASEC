"""Thin route handlers over the domain services (W2/W3; plan §4.2/§4.3).

Each handler parses one validated body, authorizes the route through
:mod:`backend.api.matrix`, calls one domain service with the request's single
connection and instant, and serializes the result. No handler holds state
between requests, none reads a global connection, and none decides ownership or
visibility on its own: the service does that against the database.

Handlers are plain ``def`` (W3 deviation from plan §4.2.4). The framework runs
them in its worker thread, so a blocking SQLite call or a blocking sender never
stops the event loop. The connection for the handler body is opened, used and
closed inside that same thread by :func:`_request_state`; the authentication
dependency opens and closes its own connection in its own thread. SQLite's
default same-thread enforcement is never relaxed.

Mutating routes accept the owner-A2 ``Idempotency-Key`` header. When it is
present, :func:`_run_mutation` stores the exact 2xx body and replays it
byte-for-byte on a retry with the same key.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend import approvals, auth, dashboards, notification_delivery, review, visibility
from backend import tasks as task_service
from backend.api import idempotency, read_models
from backend.api.errors import ApiError
from backend.api.matrix import RouteAuthorizationError, authorize_route
from backend.api.serializers import (
    accept_result_payload,
    approval_payload,
    backlog_payload,
    dashboard_admin_payload,
    dashboard_ah_payload,
    dashboard_head_payload,
    dashboard_volunteer_payload,
    decision_payload,
    edit_result_payload,
    eval_proposal_payload,
    notification_payload,
    proposal_review_payload,
    push_endpoint_payload,
    reject_result_payload,
    self_person_payload,
    session_payload,
    task_payload,
)
from backend.auth import AuthenticatedPerson
from backend.review import AssigneeSelection, ProposalEdit
from backend.tasks import TaskEdit


@dataclass
class RequestState:
    """What one handler may use: one open connection and one instant."""

    connection: sqlite3.Connection
    now: str


class OtpRequestBody(BaseModel):
    """``POST /auth/otp``; unknown fields are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    roll_number: str = Field(min_length=1, max_length=64)


class VerifyRequestBody(BaseModel):
    """``POST /auth/verify``; the code is bounded and never logged."""

    model_config = ConfigDict(extra="forbid")

    roll_number: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=12)


class AgreementBody(BaseModel):
    """``POST /tasks/{id}/agreement`` (FR-TSK-03/05)."""

    model_config = ConfigDict(extra="forbid")

    agreement: str = Field(min_length=1, max_length=16)
    assignee_person_id: int | None = Field(default=None, ge=1)


class StatusBody(BaseModel):
    """``POST /tasks/{id}/status`` (FR-TSK-06/07)."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=16)
    assignee_person_id: int | None = Field(default=None, ge=1)


class TaskEditBody(BaseModel):
    """``PATCH /tasks/{id}``; ``expected_version`` makes the edit optimistic."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    description: str | None = None
    people_needed: int | None = Field(default=None, ge=1)
    attributes: dict[str, str | None] | None = None


class ReassignBody(BaseModel):
    """``POST /tasks/{id}/reassign`` (FR-TSK-10)."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    add: list[int] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)


class CancelBody(BaseModel):
    """``POST /tasks/{id}/cancel`` (FR-TSK-08)."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=200)


class AssigneeBody(BaseModel):
    """One reviewer-supplied assignee in ``POST /proposals/{id}/edits``."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200)
    person_id: int | None = Field(default=None, ge=1)


class ProposalEditBody(BaseModel):
    """``POST /proposals/{id}/edits``; supplied fields only are applied (A2)."""

    model_config = ConfigDict(extra="forbid")

    expected_revision_number: int = Field(ge=0)
    description: str | None = None
    assignment_mode: str | None = None
    assignees: list[AssigneeBody] | None = None
    people_needed: int | None = Field(default=None, ge=1)
    people_needed_unlimited: int | None = Field(default=None, ge=0, le=1)
    task_type: str | None = None
    responsible_vertical_id: int | None = Field(default=None, ge=1)
    affected_vertical_ids: list[int] | None = None
    attributes: dict[str, str | None] | None = None


class AcceptBody(BaseModel):
    """``POST /proposals/{id}/accept`` (FR-REV-05)."""

    model_config = ConfigDict(extra="forbid")

    expected_revision_number: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=1000)
    request_message: str | None = Field(default=None, max_length=1000)


class RejectBody(BaseModel):
    """``POST /proposals/{id}/reject`` (FR-REV-07)."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)


class DecisionBody(BaseModel):
    """``POST /approvals/{id}/decision`` (FR-XV-04)."""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=16)
    message: str | None = Field(default=None, max_length=1000)


class PushEndpointBody(BaseModel):
    """``POST /push-endpoints``; the token is accepted, never echoed."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=4096)
    provider: Literal["FCM"] = "FCM"


router = APIRouter()

_DOMAIN_ERRORS = (
    task_service.TaskError,
    review.ReviewError,
    approvals.ApprovalError,
    notification_delivery.NotificationError,
)


@contextmanager
def _request_state(request: Request) -> Iterator[RequestState]:
    """Open one connection for the handler body and close it on the same thread.

    A plain context manager, not a dependency: the framework may enter and exit
    a sync-generator dependency on different worker threads, and this is the one
    place that must not happen.
    """
    application = request.app
    connection: sqlite3.Connection = application.state.connection_factory()
    try:
        yield RequestState(
            connection=connection,
            now=str(application.state.now()),
        )
    finally:
        connection.close()


def bearer_token(request: Request) -> str:
    """Read ``Authorization: Bearer <token>``; anything else is a 401."""
    header = request.headers.get("authorization")
    if header is None:
        raise ApiError("UNAUTHENTICATED")
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1].strip() == "":
        raise ApiError("UNAUTHENTICATED")
    return parts[1]


def auth_error(error: auth.AuthError) -> ApiError:
    """Map a domain sign-in error onto the fixed table."""
    if error.reason == auth.REASON_RATE_LIMITED:
        return ApiError("RATE_LIMITED")
    if error.reason == auth.REASON_INVALID_REQUEST:
        return ApiError("INVALID_REQUEST")
    return ApiError("UNAUTHENTICATED")


def require_person(request: Request) -> AuthenticatedPerson:
    """Bearer token -> ``authenticate`` -> the current roster person, or a 401.

    A plain ``def`` dependency: the framework runs it in a worker thread, and
    the connection it opens is used and closed in that same thread before the
    handler starts.
    """
    token = bearer_token(request)
    connection: sqlite3.Connection = request.app.state.connection_factory()
    try:
        person = auth.authenticate(connection, token, now=str(request.app.state.now()))
    except auth.AuthError as error:
        raise auth_error(error) from error
    finally:
        connection.close()
    # Attached for the access log and for later route-level defence in depth;
    # the value is the database row, never the request body.
    request.state.person_id = person.person_id
    return person


#: The dependency alias lives at module level because a call in a parameter
#: default is what ruff B008 forbids; ``Annotated`` keeps the annotation honest.
PersonDep = Annotated[AuthenticatedPerson, Depends(require_person)]


def _authorize(
    connection: sqlite3.Connection,
    person: AuthenticatedPerson,
    operation: str,
    subject_id: int | None = None,
) -> None:
    """One route-level authorization call per route (FR-AUT-01/06, AC-20)."""
    try:
        authorize_route(
            connection,
            person_id=person.person_id,
            operation=operation,
            subject_id=subject_id,
        )
    except RouteAuthorizationError as error:
        raise ApiError("FORBIDDEN") from error


def _api_error(error: Exception) -> ApiError:
    """Map a domain exception onto the fixed table (EIR-02, NFR-SEC-01)."""
    if isinstance(error, task_service.StaleTaskVersionError):
        # FR-TSK-12 / AC-19: the 409 carries the current snapshot, built from
        # database rows by the serializer, never from the request.
        return ApiError("CONFLICT", {"task": task_payload(error.snapshot)})
    if isinstance(
        error,
        (
            task_service.TaskNotFoundError,
            review.ProposalNotFoundError,
            approvals.ApprovalNotFoundError,
            notification_delivery.NotificationNotFoundError,
        ),
    ):
        return ApiError("NOT_FOUND")
    if isinstance(
        error,
        (
            task_service.TaskAuthorizationError,
            review.ReviewAuthorizationError,
            approvals.ApprovalAuthorizationError,
            notification_delivery.NotificationAuthorizationError,
        ),
    ):
        return ApiError("FORBIDDEN")
    if isinstance(
        error,
        (
            task_service.TaskStateError,
            task_service.TaskCapacityError,
            review.StaleRevisionError,
            review.TerminalStateError,
            review.NoChangesError,
            review.AcceptanceBlockedError,
            notification_delivery.PushEndpointConflict,
        ),
    ):
        return ApiError("CONFLICT")
    if isinstance(
        error,
        (
            task_service.InvalidAgreementError,
            review.InvalidEditError,
            approvals.InvalidDecisionError,
        ),
    ):
        return ApiError("INVALID_REQUEST")
    return ApiError("INTERNAL_ERROR")


def _execute(work: Callable[[], tuple[int, dict[str, Any]]]) -> tuple[int, dict[str, Any]]:
    """Run one service call, translating only known domain failures."""
    try:
        return work()
    except _DOMAIN_ERRORS as error:
        raise _api_error(error) from error


def _canonical(payload: dict[str, Any]) -> str:
    """The exact JSON text stored and replayed: one canonical encoding."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_response(payload: dict[str, Any], status: int) -> Response:
    return Response(
        content=_canonical(payload),
        status_code=status,
        media_type="application/json",
    )


def _run_mutation(
    request: Request,
    state: RequestState,
    person: AuthenticatedPerson,
    operation: str,
    work: Callable[[], tuple[int, dict[str, Any]]],
) -> Response:
    """Run a mutating handler under the owner-A2 idempotency contract.

    Without the header nothing changes. With it, a replay returns the stored
    status and body without calling the service, a different request under the
    same key is a 422 ``IDEMPOTENCY_KEY_REUSED``, and only a 2xx is stored.
    """
    raw_key = request.headers.get(idempotency.KEY_HEADER)
    if raw_key is None:
        status, payload = _execute(work)
        return _json_response(payload, status)

    settings = request.app.state.idempotency_settings
    key = idempotency.parse_key(raw_key, settings)
    digest = idempotency.request_hash(
        request.method, request.url.path, _raw_body(request)
    )
    try:
        stored = idempotency.lookup(
            state.connection,
            person_id=person.person_id,
            route_key=operation,
            key=key,
            request_hash=digest,
            now=state.now,
        )
    except idempotency.IdempotencyKeyReused as error:
        raise ApiError("IDEMPOTENCY_KEY_REUSED") from error
    if stored is not None:
        return Response(
            content=stored.body,
            status_code=stored.status,
            media_type="application/json",
        )

    status, payload = _execute(work)
    body = _canonical(payload)
    idempotency.store(
        state.connection,
        person_id=person.person_id,
        route_key=operation,
        key=key,
        request_hash=digest,
        status=status,
        body=body,
        now=state.now,
        settings=settings,
    )
    return Response(
        content=body, status_code=status, media_type="application/json"
    )


def _raw_body(request: Request) -> bytes:
    """The bytes the middleware cached; empty when the route has no body."""
    value = getattr(request.state, "raw_body", b"")
    return value if isinstance(value, bytes) else b""


# ---------------------------------------------------------------------------
# Public routes and sign-in
# ---------------------------------------------------------------------------


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Public liveness probe: a constant, no detail, no database access."""
    return {"status": "ok"}


@router.post("/auth/otp", status_code=202)
def request_otp(
    body: OtpRequestBody,
    request: Request,
) -> dict[str, str]:
    """Accept an OTP request. The same 202 is returned for every outcome.

    The response cannot reveal whether the roll number exists, whether the
    person is active, whether a challenge was created or whether a DM left the
    building: those outcomes live only in the private ``auth_attempt`` ledger.
    Only an unusable request value (empty after trimming) is a 422, and that
    decision is the service's, not the handler's.
    """
    with _request_state(request) as state:
        try:
            auth.request_otp(
                state.connection,
                roll_number=body.roll_number,
                sender=request.app.state.otp_sender,
                now=state.now,
                settings=request.app.state.auth_settings,
            )
        except auth.AuthError as error:
            raise auth_error(error) from error
    return {"status": "accepted"}


@router.post("/auth/verify")
def verify_otp(
    body: VerifyRequestBody,
    request: Request,
) -> dict[str, object]:
    """Exchange a valid code for one session token (returned only here)."""
    with _request_state(request) as state:
        try:
            session = auth.verify_otp(
                state.connection,
                roll_number=body.roll_number,
                code=body.code,
                now=state.now,
                settings=request.app.state.auth_settings,
            )
        except auth.AuthError as error:
            raise auth_error(error) from error
        person = auth.authenticate(state.connection, session.token, now=state.now)
        return session_payload(session, person)


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    person: PersonDep,
) -> Response:
    """Revoke the presented session; a second logout is not an error."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "AUTH_LOGOUT")
        auth.revoke_session(
            state.connection, bearer_token(request), now=state.now
        )
    return Response(status_code=204)


@router.get("/me")
def me(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """The caller's own identity, with their own contact details (NFR-SEC-03)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "ME")
        record = visibility.person_record(state.connection, person.person_id)
        fields = visibility.visible_fields(
            state.connection, person, person.person_id
        )
    if record is None:  # pragma: no cover - authenticate read the same row
        raise ApiError("UNAUTHENTICATED")
    return {"person": self_person_payload(record, fields)}


# ---------------------------------------------------------------------------
# Tasks (FR-TSK-*)
# ---------------------------------------------------------------------------


@router.get("/tasks")
def list_tasks(
    request: Request,
    person: PersonDep,
    limit: int | None = Query(default=None),
    offset: int = Query(default=0),
) -> dict[str, object]:
    """Visible tasks only, in a client-bounded page (FR-UI-02)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_LIST")
        read = request.app.state.read_settings
        page_limit = read.tasks_page_default if limit is None else limit
        if page_limit < 1 or page_limit > read.tasks_page_max or offset < 0:
            raise ApiError("INVALID_REQUEST")
        visible = task_service.visible_tasks(
            state.connection, viewer_person_id=person.person_id
        )
    page = visible[offset : offset + page_limit]
    return {
        "tasks": [task_payload(task) for task in page],
        "limit": page_limit,
        "offset": offset,
    }


@router.get("/tasks/{task_id}")
def get_task(
    task_id: int,
    request: Request,
    person: PersonDep,
) -> Response:
    """One visible task; hidden or cancelled rows are 404 (FR-TSK-11)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_VIEW", task_id)
        status, payload = _execute(
            lambda: (
                200,
                {
                    "task": task_payload(
                        task_service.get_task_summary(
                            state.connection,
                            task_id,
                            viewer_person_id=person.person_id,
                        )
                    )
                },
            )
        )
    return _json_response(payload, status)


@router.post("/tasks/{task_id}/claims", status_code=201)
def claim_task(
    task_id: int,
    request: Request,
    person: PersonDep,
) -> Response:
    """Claim one place on an OPEN task (FR-TSK-04, AC-08)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_CLAIM", task_id)

        def work() -> tuple[int, dict[str, Any]]:
            assignment_id = task_service.claim_open_task(
                state.connection, task_id, person.person_id
            )
            return 201, {"task_id": task_id, "assignment_id": assignment_id}

        return _run_mutation(request, state, person, "TASK_CLAIM", work)


@router.post("/tasks/{task_id}/agreement")
def set_agreement(
    task_id: int,
    body: AgreementBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Accept or deny one assignment (FR-TSK-03/05)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_AGREEMENT", task_id)
        assignee = (
            person.person_id
            if body.assignee_person_id is None
            else body.assignee_person_id
        )

        def work() -> tuple[int, dict[str, Any]]:
            task_service.set_agreement(
                state.connection,
                task_id,
                person.person_id,
                assignee,
                body.agreement,
            )
            return 200, {"task_id": task_id, "agreement": body.agreement}

        return _run_mutation(request, state, person, "TASK_AGREEMENT", work)


@router.post("/tasks/{task_id}/status")
def update_status(
    task_id: int,
    body: StatusBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Advance one accepted assignee's work status (FR-TSK-06/07)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_STATUS", task_id)

        def work() -> tuple[int, dict[str, Any]]:
            task_service.update_work_status(
                state.connection,
                task_id,
                person.person_id,
                body.status,
                assignee_person_id=body.assignee_person_id,
            )
            return 200, {"task_id": task_id, "work_status": body.status}

        return _run_mutation(request, state, person, "TASK_STATUS", work)


@router.patch("/tasks/{task_id}")
def edit_task(
    task_id: int,
    body: TaskEditBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Edit a task with an expected version (FR-TSK-12/16, AC-19)."""
    supplied: dict[str, object] = {}
    for name in ("description", "people_needed", "attributes"):
        if name in body.model_fields_set:
            value = getattr(body, name)
            if name == "attributes" and value is not None:
                value = tuple(sorted(value.items()))
            supplied[name] = value
    changes = TaskEdit(**supplied)
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_EDIT", task_id)

        def work() -> tuple[int, dict[str, Any]]:
            task_service.edit_task(
                state.connection,
                task_id,
                person.person_id,
                body.expected_version,
                changes,
            )
            summary = task_service.get_task_summary(
                state.connection, task_id, viewer_person_id=person.person_id
            )
            return 200, {"task": task_payload(summary)}

        status, payload = _execute(work)
    return _json_response(payload, status)


@router.post("/tasks/{task_id}/reassign")
def reassign_task(
    task_id: int,
    body: ReassignBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Add and/or remove assignees (FR-TSK-10/16)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_REASSIGN", task_id)

        def work() -> tuple[int, dict[str, Any]]:
            task_service.reassign_task(
                state.connection,
                task_id,
                person.person_id,
                body.expected_version,
                add=tuple(body.add),
                remove=tuple(body.remove),
            )
            summary = task_service.get_task_summary(
                state.connection, task_id, viewer_person_id=person.person_id
            )
            return 200, {"task": task_payload(summary)}

        return _run_mutation(request, state, person, "TASK_REASSIGN", work)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: int,
    body: CancelBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Cancel a live task and its unfinished assignments (FR-TSK-08)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "TASK_CANCEL", task_id)

        def work() -> tuple[int, dict[str, Any]]:
            task_service.cancel_task(
                state.connection, task_id, person.person_id, body.reason
            )
            return 200, {"task_id": task_id, "cancelled": True}

        return _run_mutation(request, state, person, "TASK_CANCEL", work)


# ---------------------------------------------------------------------------
# Proposal review (FR-REV-*)
# ---------------------------------------------------------------------------


@router.get("/proposals/{proposal_id}")
def get_proposal(
    proposal_id: int,
    request: Request,
    person: PersonDep,
) -> Response:
    """The Phase 6 reviewer view; the one response carrying message content."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "PROPOSAL_VIEW", proposal_id)
        status, payload = _execute(
            lambda: (
                200,
                proposal_review_payload(
                    review.get_proposal_review(
                        state.connection,
                        proposal_id,
                        actor_person_id=person.person_id,
                        config=request.app.state.config,
                    )
                ),
            )
        )
    return _json_response(payload, status)


def _proposal_edit(body: ProposalEditBody) -> ProposalEdit:
    """Translate the request body into the service's edit value."""
    supplied: dict[str, object] = {}
    for name in body.model_fields_set - {"expected_revision_number"}:
        value = getattr(body, name)
        if name == "assignees" and value is not None:
            value = tuple(
                AssigneeSelection(text=item.text, person_id=item.person_id)
                for item in value
            )
        elif name == "affected_vertical_ids" and value is not None:
            value = tuple(value)
        elif name == "attributes" and value is not None:
            value = tuple(sorted(value.items()))
        supplied[name] = value
    return ProposalEdit(**supplied)


@router.post("/proposals/{proposal_id}/edits")
def edit_proposal(
    proposal_id: int,
    body: ProposalEditBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Append one immutable revision (FR-REV-03/04)."""
    changes = _proposal_edit(body)
    with _request_state(request) as state:
        _authorize(state.connection, person, "PROPOSAL_EDIT", proposal_id)
        status, payload = _execute(
            lambda: (
                200,
                edit_result_payload(
                    review.save_proposal_edit(
                        state.connection,
                        proposal_id,
                        actor_person_id=person.person_id,
                        expected_revision_number=body.expected_revision_number,
                        changes=changes,
                        config=request.app.state.config,
                    )
                ),
            )
        )
    return _json_response(payload, status)


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: int,
    body: AcceptBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Accept a proposal and materialize its entity (FR-REV-05, AC-18)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "PROPOSAL_ACCEPT", proposal_id)

        def work() -> tuple[int, dict[str, Any]]:
            result = review.accept_proposal(
                state.connection,
                proposal_id,
                actor_person_id=person.person_id,
                expected_revision_number=body.expected_revision_number,
                note=body.note,
                request_message=body.request_message,
                config=request.app.state.config,
            )
            return 200, accept_result_payload(result)

        return _run_mutation(request, state, person, "PROPOSAL_ACCEPT", work)


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    body: RejectBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Reject a proposal, keeping it and its revisions (FR-REV-07)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "PROPOSAL_REJECT", proposal_id)

        def work() -> tuple[int, dict[str, Any]]:
            result = review.reject_proposal(
                state.connection,
                proposal_id,
                actor_person_id=person.person_id,
                note=body.note,
                config=request.app.state.config,
            )
            return 200, reject_result_payload(result)

        return _run_mutation(request, state, person, "PROPOSAL_REJECT", work)


# ---------------------------------------------------------------------------
# Approvals (FR-XV-04, FR-APR-*)
# ---------------------------------------------------------------------------


@router.get("/approvals")
def list_approvals(
    request: Request,
    person: PersonDep,
) -> Response:
    """Undecided requests visible to the caller (FR-UI-03)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "APPROVAL_LIST")
        status, payload = _execute(
            lambda: (
                200,
                {
                    "approvals": [
                        approval_payload(view)
                        for view in approvals.list_pending_approvals(
                            state.connection, actor_person_id=person.person_id
                        )
                    ]
                },
            )
        )
    return _json_response(payload, status)


@router.get("/approvals/{request_id}")
def get_approval(
    request_id: int,
    request: Request,
    person: PersonDep,
) -> Response:
    """One approval request the caller may see."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "APPROVAL_READ", request_id)
        status, payload = _execute(
            lambda: (
                200,
                {
                    "approval": approval_payload(
                        approvals.get_approval_request(
                            state.connection,
                            request_id,
                            actor_person_id=person.person_id,
                        )
                    )
                },
            )
        )
    return _json_response(payload, status)


@router.post("/approvals/{request_id}/decision")
def decide_approval(
    request_id: int,
    body: DecisionBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Resolve one request; the first decision wins (FR-XV-04, AC-15)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "APPROVAL_DECIDE", request_id)

        def work() -> tuple[int, dict[str, Any]]:
            result = approvals.decide_request(
                state.connection,
                request_id,
                actor_person_id=person.person_id,
                decision=body.decision,
                message=body.message,
            )
            return 200, decision_payload(result)

        return _run_mutation(request, state, person, "APPROVAL_DECIDE", work)


# ---------------------------------------------------------------------------
# Notifications and push endpoints (FR-NOT-07)
# ---------------------------------------------------------------------------


@router.get("/notifications")
def list_notifications(
    request: Request,
    person: PersonDep,
    unread_only: bool = Query(default=False),
) -> dict[str, object]:
    """Only the caller's own notifications, newest last (NFR-SEC-03)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "NOTIFICATION_LIST")
        views = notification_delivery.list_notifications(
            state.connection,
            person_id=person.person_id,
            unread_only=unread_only,
        )
    return {"notifications": [notification_payload(view) for view in views]}


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    request: Request,
    person: PersonDep,
) -> Response:
    """Mark the caller's own notification read; a repeat is not an error."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "NOTIFICATION_READ", notification_id)
        status, payload = _execute(
            lambda: (
                200,
                {
                    "notification_id": notification_id,
                    "read": notification_delivery.mark_read(
                        state.connection,
                        notification_id,
                        person_id=person.person_id,
                    ),
                },
            )
        )
    return _json_response(payload, status)


@router.post("/push-endpoints", status_code=201)
def register_push_endpoint(
    body: PushEndpointBody,
    request: Request,
    person: PersonDep,
) -> Response:
    """Register (or refresh) the caller's own push endpoint."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "PUSH_ENDPOINT_REGISTER")

        def work() -> tuple[int, dict[str, Any]]:
            endpoint_id = notification_delivery.register_endpoint(
                state.connection,
                person_id=person.person_id,
                token=body.token,
                provider=body.provider,
                now=state.now,
            )
            return 201, push_endpoint_payload(endpoint_id, active=True)

        return _run_mutation(request, state, person, "PUSH_ENDPOINT_REGISTER", work)


@router.delete("/push-endpoints/{endpoint_id}")
def deactivate_push_endpoint(
    endpoint_id: int,
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """Deactivate the caller's own endpoint; a repeat is not an error."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "PUSH_ENDPOINT_DEACTIVATE", endpoint_id)
        notification_delivery.deactivate_endpoint(
            state.connection, endpoint_id, person_id=person.person_id
        )
    return push_endpoint_payload(endpoint_id, active=False)


# ---------------------------------------------------------------------------
# Dashboards (FR-UI-02..05, NFR-SEC-03, NFR-REL-06)
# ---------------------------------------------------------------------------


@router.get("/dashboard/volunteer")
def volunteer_dashboard(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """FR-UI-02: own tasks, claimable open tasks and own notifications."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "DASHBOARD_VOLUNTEER")
        view = dashboards.volunteer_dashboard(state.connection, actor=person)
    return dashboard_volunteer_payload(view)


@router.get("/dashboard/ah")
def ah_dashboard(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """FR-UI-03: review queue, created tasks, direct reports, approvals."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "DASHBOARD_AH")
        view = dashboards.ah_dashboard(state.connection, actor=person)
    return dashboard_ah_payload(view)


@router.get("/dashboard/head")
def head_dashboard(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """FR-UI-04: the vertical's tasks and people, plus affected cross-vertical work."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "DASHBOARD_HEAD")
        view = dashboards.head_dashboard(state.connection, actor=person)
    return dashboard_head_payload(view)


@router.get("/dashboard/admin")
def admin_dashboard(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """FR-UI-05: backlog, attempts, denials, evaluation and config key names."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "DASHBOARD_ADMIN")
        view = dashboards.admin_dashboard(
            state.connection,
            actor=person,
            config=request.app.state.config,
        )
    return dashboard_admin_payload(view)


# ---------------------------------------------------------------------------
# Admin read routes (FR-UI-05, NFR-REL-06)
# ---------------------------------------------------------------------------


def _admin_page(
    request: Request,
    limit: int | None,
    offset: int,
) -> int:
    """Validate one Admin page request against the configured cap."""
    read = request.app.state.read_settings
    page_limit = read.admin_read_limit if limit is None else limit
    if page_limit < 1 or page_limit > read.admin_read_limit or offset < 0:
        raise ApiError("INVALID_REQUEST")
    return page_limit


@router.get("/admin/denials")
def admin_denials(
    request: Request,
    person: PersonDep,
    limit: int | None = Query(default=None),
    offset: int = Query(default=0),
) -> dict[str, object]:
    """The recorded denial ledger, newest first (FR-AUT-06)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "ADMIN_DENIALS")
        page_limit = _admin_page(request, limit, offset)
        rows = read_models.denials_page(
            state.connection, limit=page_limit, offset=offset
        )
    return {"denials": list(rows), "limit": page_limit, "offset": offset}


@router.get("/admin/backlog")
def admin_backlog(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """NFR-REL-06: unprocessed bucket count and the oldest bucket's age."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "ADMIN_BACKLOG")
        summary = dashboards.backlog_summary(state.connection)
    return {"backlog": backlog_payload(summary)}


@router.get("/admin/attempts")
def admin_attempts(
    request: Request,
    person: PersonDep,
    limit: int | None = Query(default=None),
    offset: int = Query(default=0),
) -> dict[str, object]:
    """The processing-attempt ledger, newest first, without error text."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "ADMIN_ATTEMPTS")
        page_limit = _admin_page(request, limit, offset)
        rows = read_models.attempts_page(
            state.connection, limit=page_limit, offset=offset
        )
    return {"attempts": list(rows), "limit": page_limit, "offset": offset}


@router.get("/admin/config")
def admin_config(
    request: Request,
    person: PersonDep,
) -> dict[str, object]:
    """Configuration names with every value redacted (NFR-SEC-01)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "ADMIN_CONFIG")
    return {"config": request.app.state.config.as_public_dict()}


@router.get("/admin/evaluation/export")
def admin_evaluation_export(
    request: Request,
    person: PersonDep,
) -> Response:
    """Labelled review data for evaluation (FR-REV-08)."""
    with _request_state(request) as state:
        _authorize(state.connection, person, "ADMIN_EVALUATION_EXPORT")
        status, payload = _execute(
            lambda: (
                200,
                {
                    "proposals": [
                        eval_proposal_payload(item)
                        for item in review.list_reviewed_proposals_for_eval(
                            state.connection,
                            actor_person_id=person.person_id,
                            config=request.app.state.config,
                        )
                    ]
                },
            )
        )
    return _json_response(payload, status)
