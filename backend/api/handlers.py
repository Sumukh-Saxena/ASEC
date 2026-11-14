"""Thin route handlers and the per-request dependency seam (W2; plan §4.2.4-4.2.6).

Each handler parses one validated body, calls one domain service with the
request's single connection and instant, and serializes the result. No handler
holds state between requests, none reads a global connection, and none decides
authorization on its own: the service does that against the database.

Handlers run on the event-loop thread so the per-request SQLite connection is
never used from a second thread (``sqlite3`` default ``check_same_thread``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend import auth
from backend.api.errors import ApiError
from backend.api.serializers import person_payload, session_payload
from backend.auth import AuthenticatedPerson


@dataclass
class RequestState:
    """What one request may use: one open connection and one instant."""

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


router = APIRouter()


async def _open_request_state(request: Request) -> AsyncIterator[RequestState]:
    """Open exactly one connection for this request and close it afterwards.

    ``connection_factory`` is the composition root's choice (the CLI passes
    ``backend.db.connection.connect``, so WAL, foreign keys and the busy timeout
    are identical to every other process), and ``now`` is read once so every
    service call in the request sees the same instant.
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


async def require_person(
    request: Request,
    state: Annotated[RequestState, Depends(_open_request_state)],
) -> AuthenticatedPerson:
    """Bearer token -> ``authenticate`` -> the current roster person, or a 401."""
    token = bearer_token(request)
    try:
        person = auth.authenticate(state.connection, token, now=state.now)
    except auth.AuthError as error:
        raise auth_error(error) from error
    # Attached for the access log and for later route-level defence in depth;
    # the value is the database row, never the request body.
    request.state.person_id = person.person_id
    return person


#: The dependency aliases live at module level because a call in a parameter
#: default is what ruff B008 forbids; ``Annotated`` keeps the annotation honest.
StateDep = Annotated[RequestState, Depends(_open_request_state)]
PersonDep = Annotated[AuthenticatedPerson, Depends(require_person)]


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Public liveness probe: a constant, no detail, no database access."""
    return {"status": "ok"}


@router.post("/auth/otp", status_code=202)
async def request_otp(
    body: OtpRequestBody,
    request: Request,
    state: StateDep,
) -> dict[str, str]:
    """Accept an OTP request. The same 202 is returned for every outcome.

    The response cannot reveal whether the roll number exists, whether the
    person is active, whether a challenge was created or whether a DM left the
    building: those outcomes live only in the private ``auth_attempt`` ledger.
    Only an unusable request value (empty after trimming) is a 422, and that
    decision is the service's, not the handler's.
    """
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
async def verify_otp(
    body: VerifyRequestBody,
    request: Request,
    state: StateDep,
) -> dict[str, object]:
    """Exchange a valid code for one session token (returned only here)."""
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
async def logout(
    request: Request,
    state: StateDep,
    _person: PersonDep,
) -> Response:
    """Revoke the presented session; a second logout is not an error."""
    auth.revoke_session(state.connection, bearer_token(request), now=state.now)
    return Response(status_code=204)


@router.get("/me")
async def me(
    person: PersonDep,
) -> dict[str, object]:
    """The caller's own identity, always from the current roster row."""
    return {"person": person_payload(person)}
