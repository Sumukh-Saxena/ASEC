"""The route authorization matrix (W3; FR-AUT-01/02/04/06, AC-20).

Every route the app registers has exactly one row here, and every operation is
reachable through exactly one route. ``authorize_route`` is the single
route-level gate: it re-reads the caller's current role from the roster, records
a denial in ``authorization_denial`` before raising, and never grants anything
the domain service would refuse. The service remains authoritative for
ownership, vertical membership and state; this table answers only "may this
role touch this route at all?".

Reasons are the stable route-level vocabulary in the plan:
``NOT_ROLE``, ``NOT_OWNER``, ``NOT_VERTICAL``, ``INACTIVE``, ``NOT_RECIPIENT``.
A service keeps its own finer reasons (for example ``NOT_IN_VERTICAL``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from backend.authorization import record_denial

#: The four roster roles (0016's CHECK makes any other value unreachable).
ALL_ROLES = frozenset({"Admin", "Head", "AH", "Volunteer"})
REVIEW_ROLES = frozenset({"Admin", "Head", "AH"})
VOLUNTEER_ROLES = frozenset({"Volunteer"})
ADMIN_ROLES = frozenset({"Admin"})

REASON_NOT_ROLE = "NOT_ROLE"
REASON_INACTIVE = "INACTIVE"

#: Subject type recorded with a route-level denial.
SUBJECT_NONE = "NONE"
SUBJECT_PERSON = "PERSON"
SUBJECT_SESSION = "SESSION"
SUBJECT_TASK = "TASK"
SUBJECT_PROPOSAL = "PROPOSAL"
SUBJECT_APPROVAL_REQUEST = "APPROVAL_REQUEST"
SUBJECT_NOTIFICATION = "NOTIFICATION"
SUBJECT_PUSH_ENDPOINT = "PUSH_ENDPOINT"
SUBJECT_ADMIN = "ADMIN"


class RouteAuthorizationError(Exception):
    """The caller's current role may not use this route (recorded first)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Operation:
    """One classified route surface.

    ``scope`` is the plain-language statement of who the row admits; it is
    documentation that the tests keep non-empty, not a rule the code reads.
    """

    name: str
    scope: str
    subject_type: str
    allowed_roles: frozenset[str]
    denial_reason: str
    public: bool = False


def _role_operation(
    name: str,
    scope: str,
    subject_type: str,
    roles: frozenset[str],
) -> Operation:
    return Operation(
        name=name,
        scope=scope,
        subject_type=subject_type,
        allowed_roles=roles,
        denial_reason=REASON_NOT_ROLE,
    )


def _public_operation(name: str, scope: str, subject_type: str) -> Operation:
    return Operation(
        name=name,
        scope=scope,
        subject_type=subject_type,
        allowed_roles=frozenset(),
        denial_reason=REASON_NOT_ROLE,
        public=True,
    )


_OPERATION_LIST: tuple[Operation, ...] = (
    _public_operation("HEALTH", "any caller; liveness only", SUBJECT_NONE),
    _public_operation("AUTH_OTP", "any caller; sign-in request", SUBJECT_PERSON),
    _public_operation("AUTH_VERIFY", "any caller; code exchange", SUBJECT_PERSON),
    _role_operation(
        "AUTH_LOGOUT", "any signed-in person revokes their own session",
        SUBJECT_SESSION, ALL_ROLES,
    ),
    _role_operation(
        "ME", "any signed-in person reads their own identity",
        SUBJECT_PERSON, ALL_ROLES,
    ),
    _role_operation(
        "TASK_LIST", "every role; the service filters by visibility",
        SUBJECT_TASK, ALL_ROLES,
    ),
    _role_operation(
        "TASK_VIEW", "every role; the service filters by visibility",
        SUBJECT_TASK, ALL_ROLES,
    ),
    _role_operation(
        "TASK_CLAIM", "a Volunteer claims an open task in their vertical",
        SUBJECT_TASK, VOLUNTEER_ROLES,
    ),
    _role_operation(
        "TASK_AGREEMENT", "a Volunteer answers their own assignment",
        SUBJECT_TASK, VOLUNTEER_ROLES,
    ),
    _role_operation(
        "TASK_STATUS", "a Volunteer advances their own work status",
        SUBJECT_TASK, VOLUNTEER_ROLES,
    ),
    _role_operation(
        "TASK_EDIT", "every role; the service checks task ownership",
        SUBJECT_TASK, ALL_ROLES,
    ),
    _role_operation(
        "TASK_REASSIGN", "every role; the service checks task ownership",
        SUBJECT_TASK, ALL_ROLES,
    ),
    _role_operation(
        "TASK_CANCEL", "every role; the service checks task ownership",
        SUBJECT_TASK, ALL_ROLES,
    ),
    _role_operation(
        "PROPOSAL_VIEW", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, REVIEW_ROLES,
    ),
    _role_operation(
        "PROPOSAL_EDIT", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, REVIEW_ROLES,
    ),
    _role_operation(
        "PROPOSAL_ACCEPT", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, REVIEW_ROLES,
    ),
    _role_operation(
        "PROPOSAL_REJECT", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, REVIEW_ROLES,
    ),
    _role_operation(
        "APPROVAL_LIST", "an approval recipient (AH/Head) or an Admin",
        SUBJECT_APPROVAL_REQUEST, REVIEW_ROLES,
    ),
    _role_operation(
        "APPROVAL_READ", "an approval recipient (AH/Head) or an Admin",
        SUBJECT_APPROVAL_REQUEST, REVIEW_ROLES,
    ),
    _role_operation(
        "APPROVAL_DECIDE", "an approval recipient (AH/Head) or an Admin",
        SUBJECT_APPROVAL_REQUEST, REVIEW_ROLES,
    ),
    _role_operation(
        "NOTIFICATION_LIST", "any signed-in person; the service filters by owner",
        SUBJECT_NOTIFICATION, ALL_ROLES,
    ),
    _role_operation(
        "NOTIFICATION_READ", "any signed-in person; the service checks the owner",
        SUBJECT_NOTIFICATION, ALL_ROLES,
    ),
    _role_operation(
        "PUSH_ENDPOINT_REGISTER", "any signed-in person registers their own device",
        SUBJECT_PUSH_ENDPOINT, ALL_ROLES,
    ),
    _role_operation(
        "PUSH_ENDPOINT_DEACTIVATE", "any signed-in person deactivates their own device",
        SUBJECT_PUSH_ENDPOINT, ALL_ROLES,
    ),
    _role_operation(
        "ADMIN_DENIALS", "an Admin reads the denial ledger",
        SUBJECT_ADMIN, ADMIN_ROLES,
    ),
    _role_operation(
        "ADMIN_ATTEMPTS", "an Admin reads the processing-attempt ledger",
        SUBJECT_ADMIN, ADMIN_ROLES,
    ),
    _role_operation(
        "ADMIN_CONFIG", "an Admin reads the redacted configuration names",
        SUBJECT_ADMIN, ADMIN_ROLES,
    ),
    _role_operation(
        "ADMIN_EVALUATION_EXPORT", "an Admin exports labelled review data",
        SUBJECT_ADMIN, ADMIN_ROLES,
    ),
)

OPERATIONS: Mapping[str, Operation] = {
    operation.name: operation for operation in _OPERATION_LIST
}

#: One row per registered (method, path). Keys are exactly what Starlette
#: reports, including the ``{parameter}`` style.
ROUTE_OPERATIONS: Mapping[tuple[str, str], str] = {
    ("GET", "/healthz"): "HEALTH",
    ("POST", "/auth/otp"): "AUTH_OTP",
    ("POST", "/auth/verify"): "AUTH_VERIFY",
    ("POST", "/auth/logout"): "AUTH_LOGOUT",
    ("GET", "/me"): "ME",
    ("GET", "/tasks"): "TASK_LIST",
    ("GET", "/tasks/{task_id}"): "TASK_VIEW",
    ("POST", "/tasks/{task_id}/claims"): "TASK_CLAIM",
    ("POST", "/tasks/{task_id}/agreement"): "TASK_AGREEMENT",
    ("POST", "/tasks/{task_id}/status"): "TASK_STATUS",
    ("PATCH", "/tasks/{task_id}"): "TASK_EDIT",
    ("POST", "/tasks/{task_id}/reassign"): "TASK_REASSIGN",
    ("POST", "/tasks/{task_id}/cancel"): "TASK_CANCEL",
    ("GET", "/proposals/{proposal_id}"): "PROPOSAL_VIEW",
    ("POST", "/proposals/{proposal_id}/edits"): "PROPOSAL_EDIT",
    ("POST", "/proposals/{proposal_id}/accept"): "PROPOSAL_ACCEPT",
    ("POST", "/proposals/{proposal_id}/reject"): "PROPOSAL_REJECT",
    ("GET", "/approvals"): "APPROVAL_LIST",
    ("GET", "/approvals/{request_id}"): "APPROVAL_READ",
    ("POST", "/approvals/{request_id}/decision"): "APPROVAL_DECIDE",
    ("GET", "/notifications"): "NOTIFICATION_LIST",
    ("POST", "/notifications/{notification_id}/read"): "NOTIFICATION_READ",
    ("POST", "/push-endpoints"): "PUSH_ENDPOINT_REGISTER",
    ("DELETE", "/push-endpoints/{endpoint_id}"): "PUSH_ENDPOINT_DEACTIVATE",
    ("GET", "/admin/denials"): "ADMIN_DENIALS",
    ("GET", "/admin/attempts"): "ADMIN_ATTEMPTS",
    ("GET", "/admin/config"): "ADMIN_CONFIG",
    ("GET", "/admin/evaluation/export"): "ADMIN_EVALUATION_EXPORT",
}


def authorize_route(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    operation: str,
    subject_id: int | None = None,
) -> None:
    """Admit or refuse one route for the caller's *current* roster role.

    The role is read from ``person`` on this connection, never from the token or
    a request header (FR-AUT-05). A refusal is recorded in its own committed
    transaction before :class:`RouteAuthorizationError` is raised (FR-AUT-06).
    """
    row = OPERATIONS[operation]
    if row.public:
        return

    person = connection.execute(
        "SELECT role, active FROM person WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None or int(person["active"]) != 1:
        record_denial(
            connection,
            actor_person_id=person_id,
            operation=row.name,
            subject_type=row.subject_type,
            subject_id=subject_id,
            reason=REASON_INACTIVE,
        )
        raise RouteAuthorizationError(REASON_INACTIVE)

    role = str(person["role"]).strip()
    if role not in row.allowed_roles:
        record_denial(
            connection,
            actor_person_id=person_id,
            operation=row.name,
            subject_type=row.subject_type,
            subject_id=subject_id,
            reason=row.denial_reason,
        )
        raise RouteAuthorizationError(row.denial_reason)
