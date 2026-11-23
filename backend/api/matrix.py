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
ROLE_ORDER: tuple[str, ...] = ("Admin", "Head", "AH", "Volunteer")
ALL_ROLES = frozenset(ROLE_ORDER)
REVIEW_ROLES = frozenset({"Admin", "Head", "AH"})
VOLUNTEER_ROLES = frozenset({"Volunteer"})
ADMIN_ROLES = frozenset({"Admin"})

REASON_NOT_ROLE = "NOT_ROLE"
REASON_NOT_OWNER = "NOT_OWNER"
REASON_NOT_VERTICAL = "NOT_VERTICAL"
REASON_INACTIVE = "INACTIVE"
REASON_NOT_RECIPIENT = "NOT_RECIPIENT"

#: The only reason tokens a route-level denial may carry (plan §4.4).
STABLE_REASONS = frozenset(
    {
        REASON_NOT_ROLE,
        REASON_NOT_OWNER,
        REASON_NOT_VERTICAL,
        REASON_INACTIVE,
        REASON_NOT_RECIPIENT,
    }
)

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


class CellKind:
    """The SRS matrix cell vocabulary, normalised to five machine values."""

    YES = "YES"
    OWN = "OWN"
    SCOPE = "SCOPE"
    OVERRIDE = "OVERRIDE"
    NO = "NO"
    ALL = frozenset({YES, OWN, SCOPE, OVERRIDE, NO})


@dataclass(frozen=True)
class Cell:
    """One FR-AUT-04 cell: the exact wording plus its normalised kind.

    ``needs_owner`` marks a cell whose SRS wording has more than one reading.
    The report lists those cells; the code uses the more restrictive reading.
    """

    kind: str
    wording: str
    needs_owner: bool = False

    def __post_init__(self) -> None:
        if self.kind not in CellKind.ALL:
            raise ValueError(f"unknown matrix cell kind {self.kind!r}")
        if self.wording.strip() == "":
            raise ValueError("a matrix cell needs its SRS wording")


@dataclass(frozen=True)
class MatrixOperation:
    """One row of the FR-AUT-04 table and the routes that implement it."""

    name: str
    srs_wording: str
    subject_type: str
    cells: Mapping[str, Cell]
    route_operations: tuple[str, ...] = ()
    note: str = ""


def _cells(
    admin: Cell,
    head: Cell,
    ah: Cell,
    volunteer: Cell,
) -> Mapping[str, Cell]:
    return {"Admin": admin, "Head": head, "AH": ah, "Volunteer": volunteer}


#: The twelve FR-AUT-04 rows, in SRS order. ``O12-5`` is binding: the cells
#: added by O7-3, O7-5 and O8-1 are part of the production baseline, so Admin's
#: "Accept or deny an assigned task; update own work status" cell is OVERRIDE
#: (O7-3/O7-5) even though the printed SRS table shows "—".
FR_AUT_OPERATIONS: tuple[MatrixOperation, ...] = (
    MatrixOperation(
        name="REVIEW_OWN_PROPOSAL",
        srs_wording="Receive and review own proposals",
        subject_type=SUBJECT_PROPOSAL,
        cells=_cells(
            Cell(CellKind.OVERRIDE, "Override"),
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("PROPOSAL_VIEW",),
        note="'Own' is the proposal's reviewer/sender (Phase 6 routing).",
    ),
    MatrixOperation(
        name="DECIDE_PROPOSAL",
        srs_wording="Edit, accept or reject a proposal",
        subject_type=SUBJECT_PROPOSAL,
        cells=_cells(
            Cell(CellKind.OVERRIDE, "Override"),
            Cell(CellKind.OWN, "Own proposals"),
            Cell(CellKind.OWN, "Own proposals"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("PROPOSAL_EDIT", "PROPOSAL_ACCEPT", "PROPOSAL_REJECT"),
        note="'Own proposals' = the proposal assigned to the actor as reviewer.",
    ),
    MatrixOperation(
        name="CREATE_TASK_OWN_VERTICAL",
        srs_wording="Create a task in own vertical (by accepting)",
        subject_type=SUBJECT_PROPOSAL,
        cells=_cells(
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("PROPOSAL_ACCEPT",),
        note=(
            "NEEDS-OWNER: 'own vertical' can mean (i) the proposal's source"
            " vertical must be one of the actor's current verticals, or (ii)"
            " the actor owns the proposal and the task is created in its source"
            " vertical. The service implements (ii); (i) would be a new domain"
            " rule. The more restrictive test uses (ii) and a reviewer in the"
            " source vertical."
        ),
    ),
    MatrixOperation(
        name="RAISE_CROSS_VERTICAL",
        srs_wording="Raise a cross-vertical task",
        subject_type=SUBJECT_PROPOSAL,
        cells=_cells(
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.YES, "Directly"),
            Cell(CellKind.SCOPE, "By approval"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("PROPOSAL_ACCEPT",),
        note="Head accepts directly; AH acceptance opens an approval request.",
    ),
    MatrixOperation(
        name="DECIDE_CROSS_VERTICAL",
        srs_wording="Approve or deny a cross-vertical request",
        subject_type=SUBJECT_APPROVAL_REQUEST,
        cells=_cells(
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.SCOPE, "Target vertical"),
            Cell(CellKind.SCOPE, "Target vertical"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("APPROVAL_LIST", "APPROVAL_READ", "APPROVAL_DECIDE"),
        note=(
            "O8-1: Admin may decide any pending request without being a"
            " recipient. Listing and reading a request are part of this"
            " surface: the decision is impossible without seeing it."
        ),
    ),
    MatrixOperation(
        name="MANAGE_TASK",
        srs_wording="Edit, reassign or cancel a task",
        subject_type=SUBJECT_TASK,
        cells=_cells(
            Cell(CellKind.YES, "Yes"),
            Cell(
                CellKind.SCOPE,
                "Tasks they created, or whose responsible vertical is theirs",
            ),
            Cell(CellKind.SCOPE, "Tasks they created"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("TASK_EDIT", "TASK_REASSIGN", "TASK_CANCEL"),
    ),
    MatrixOperation(
        name="CLAIM_TASK",
        srs_wording="Claim an open task",
        subject_type=SUBJECT_TASK,
        cells=_cells(
            Cell(CellKind.NO, "—"),
            Cell(CellKind.NO, "—"),
            Cell(CellKind.NO, "—"),
            Cell(CellKind.SCOPE, "Own vertical"),
        ),
        route_operations=("TASK_CLAIM",),
    ),
    MatrixOperation(
        name="ASSIGNMENT_ACTION",
        srs_wording="Accept or deny an assigned task; update own work status",
        subject_type=SUBJECT_TASK,
        cells=_cells(
            Cell(CellKind.OVERRIDE, "— (O7-3/O7-5: override)"),
            Cell(CellKind.SCOPE, "Own assignments"),
            Cell(CellKind.SCOPE, "Own assignments"),
            Cell(CellKind.SCOPE, "Own assignments"),
        ),
        route_operations=("TASK_AGREEMENT", "TASK_STATUS"),
        note=(
            "The printed SRS cell for Admin is '—'; O7-3 (accept/deny on an"
            " assignee's behalf) and O7-5 (work-status override) add the Admin"
            " override cell, and O12-5 approves those additions."
        ),
    ),
    MatrixOperation(
        name="SEE_PROGRESS",
        srs_wording="See per-volunteer progress",
        subject_type=SUBJECT_TASK,
        cells=_cells(
            Cell(CellKind.YES, "All"),
            Cell(CellKind.SCOPE, "Own vertical"),
            Cell(
                CellKind.SCOPE,
                "Tasks they created, and volunteers under them",
            ),
            Cell(CellKind.SCOPE, "Own tasks only"),
        ),
        route_operations=(
            "TASK_LIST",
            "TASK_VIEW",
            "DASHBOARD_VOLUNTEER",
            "DASHBOARD_AH",
            "DASHBOARD_HEAD",
        ),
        note=(
            "Task visibility supplies the task half; the hierarchy half"
            " ('volunteers under them') is implemented by"
            " backend.visibility and tested in test_api_visibility.py. The"
            " three role dashboards read the same row, each restricted to its"
            " own role plus Admin's Yes/All cell."
        ),
    ),
    MatrixOperation(
        name="PUSH_ANNOUNCEMENT",
        srs_wording="Push announcements",
        subject_type=SUBJECT_PROPOSAL,
        cells=_cells(
            Cell(
                CellKind.NO,
                "—",
            ),
            Cell(CellKind.SCOPE, "To own vertical"),
            Cell(CellKind.SCOPE, "To volunteers under them"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=("PROPOSAL_ACCEPT",),
        note=(
            "There is no separate broadcast route; accepting the announcement"
            " is the operation. Owner decision A (Phase 12 D1): the SRS '—'"
            " stands for Admin, so"
            " :func:`authorize_proposal_acceptance` refuses an Admin's attempt"
            " to accept an ANNOUNCEMENT proposal and records the denial against"
            " this operation. All other Admin overrides are unchanged."
        ),
    ),
    MatrixOperation(
        name="VIEW_INTERNALS",
        srs_wording="View processing records, model results, configuration",
        subject_type=SUBJECT_ADMIN,
        cells=_cells(
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.NO, "—"),
            Cell(CellKind.NO, "—"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=(
            "ADMIN_DENIALS",
            "ADMIN_ATTEMPTS",
            "ADMIN_CONFIG",
            "ADMIN_EVALUATION_EXPORT",
            "DASHBOARD_ADMIN",
            "ADMIN_BACKLOG",
        ),
    ),
    MatrixOperation(
        name="CHANGE_ROSTER",
        srs_wording="Change roster data",
        subject_type=SUBJECT_PERSON,
        cells=_cells(
            Cell(CellKind.YES, "Yes"),
            Cell(CellKind.NO, "—"),
            Cell(CellKind.NO, "—"),
            Cell(CellKind.NO, "—"),
        ),
        route_operations=(),
        note=(
            "No MVP route: O12-4/FR-ROS-02. The matrix cell is data-only; the"
            " tests assert the gap rather than inventing a route."
        ),
    ),
)

MATRIX_OPERATIONS: Mapping[str, MatrixOperation] = {
    operation.name: operation for operation in FR_AUT_OPERATIONS
}


def _matrix_roles(*names: str) -> frozenset[str]:
    """The union of roles admitted by the named FR-AUT-04 rows."""
    roles: set[str] = set()
    for name in names:
        for role, cell in MATRIX_OPERATIONS[name].cells.items():
            if cell.kind != CellKind.NO:
                roles.add(role)
    return frozenset(roles)


@dataclass(frozen=True)
class Operation:
    """One classified route surface.

    ``scope`` is the plain-language statement of who the row admits; it is
    documentation that the tests keep non-empty, not a rule the code reads.
    ``matrix_operations`` names the FR-AUT-04 rows this route implements; the
    domain rows derive their role set from the matrix, so there is no second
    copy of the 12x4 table.
    """

    name: str
    scope: str
    subject_type: str
    allowed_roles: frozenset[str]
    denial_reason: str
    matrix_operations: tuple[str, ...] = ()
    public: bool = False


def _role_operation(
    name: str,
    scope: str,
    subject_type: str,
    roles: frozenset[str] | None = None,
    *,
    matrix_operations: tuple[str, ...] = (),
) -> Operation:
    if roles is None:
        roles = _matrix_roles(*matrix_operations)
    return Operation(
        name=name,
        scope=scope,
        subject_type=subject_type,
        allowed_roles=roles,
        denial_reason=REASON_NOT_ROLE,
        matrix_operations=matrix_operations,
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


#: Routes that no FR-AUT-04 row describes: they either are public, act on the
#: caller's own session/identity, or act on the caller's own notifications and
#: devices. They keep an explicit role set instead of deriving one from the
#: matrix, so the matrix is exactly the twelve SRS rows and nothing else.
NON_MATRIX_ROUTE_OPERATIONS: frozenset[str] = frozenset(
    {
        "HEALTH",
        "AUTH_OTP",
        "AUTH_VERIFY",
        "AUTH_LOGOUT",
        "ME",
        "NOTIFICATION_LIST",
        "NOTIFICATION_READ",
        "PUSH_ENDPOINT_REGISTER",
        "PUSH_ENDPOINT_DEACTIVATE",
        "ADMIN_SYSTEM",
        "ADMIN_SYSTEM_PAUSE",
        "ADMIN_SYSTEM_RESUME",
    }
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
        SUBJECT_TASK, matrix_operations=("SEE_PROGRESS",),
    ),
    _role_operation(
        "TASK_VIEW", "every role; the service filters by visibility",
        SUBJECT_TASK, matrix_operations=("SEE_PROGRESS",),
    ),
    _role_operation(
        "TASK_CLAIM", "a Volunteer claims an open task in their vertical",
        SUBJECT_TASK, matrix_operations=("CLAIM_TASK",),
    ),
    _role_operation(
        "TASK_AGREEMENT", "every role answers their own assignment",
        SUBJECT_TASK, matrix_operations=("ASSIGNMENT_ACTION",),
    ),
    _role_operation(
        "TASK_STATUS", "every role advances their own work status",
        SUBJECT_TASK, matrix_operations=("ASSIGNMENT_ACTION",),
    ),
    _role_operation(
        "TASK_EDIT", "Admin, or the task's creator/responsible Head/AH",
        SUBJECT_TASK, matrix_operations=("MANAGE_TASK",),
    ),
    _role_operation(
        "TASK_REASSIGN", "Admin, or the task's creator/responsible Head/AH",
        SUBJECT_TASK, matrix_operations=("MANAGE_TASK",),
    ),
    _role_operation(
        "TASK_CANCEL", "Admin, or the task's creator/responsible Head/AH",
        SUBJECT_TASK, matrix_operations=("MANAGE_TASK",),
    ),
    _role_operation(
        "PROPOSAL_VIEW", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, matrix_operations=("REVIEW_OWN_PROPOSAL",),
    ),
    _role_operation(
        "PROPOSAL_EDIT", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, matrix_operations=("DECIDE_PROPOSAL",),
    ),
    _role_operation(
        "PROPOSAL_ACCEPT",
        "the reviewer or an Admin; the service re-checks scope and approval",
        SUBJECT_PROPOSAL,
        matrix_operations=(
            "DECIDE_PROPOSAL",
            "CREATE_TASK_OWN_VERTICAL",
            "RAISE_CROSS_VERTICAL",
            "PUSH_ANNOUNCEMENT",
        ),
    ),
    _role_operation(
        "PROPOSAL_REJECT", "the reviewer or an Admin; the service re-checks",
        SUBJECT_PROPOSAL, matrix_operations=("DECIDE_PROPOSAL",),
    ),
    _role_operation(
        "APPROVAL_LIST", "an approval recipient (AH/Head) or an Admin",
        SUBJECT_APPROVAL_REQUEST, matrix_operations=("DECIDE_CROSS_VERTICAL",),
    ),
    _role_operation(
        "APPROVAL_READ", "an approval recipient (AH/Head) or an Admin",
        SUBJECT_APPROVAL_REQUEST, matrix_operations=("DECIDE_CROSS_VERTICAL",),
    ),
    _role_operation(
        "APPROVAL_DECIDE", "an approval recipient (AH/Head) or an Admin",
        SUBJECT_APPROVAL_REQUEST, matrix_operations=("DECIDE_CROSS_VERTICAL",),
    ),
    _role_operation(
        "DASHBOARD_VOLUNTEER",
        "the Volunteer dashboard; Admin may read every dashboard",
        SUBJECT_TASK,
        frozenset({"Admin", "Volunteer"}),
        matrix_operations=("SEE_PROGRESS",),
    ),
    _role_operation(
        "DASHBOARD_AH",
        "the AH dashboard; Admin may read every dashboard",
        SUBJECT_TASK,
        frozenset({"Admin", "AH"}),
        matrix_operations=("SEE_PROGRESS",),
    ),
    _role_operation(
        "DASHBOARD_HEAD",
        "the Head dashboard; Admin may read every dashboard",
        SUBJECT_TASK,
        frozenset({"Admin", "Head"}),
        matrix_operations=("SEE_PROGRESS",),
    ),
    _role_operation(
        "DASHBOARD_ADMIN",
        "the Admin dashboard (FR-UI-05)",
        SUBJECT_ADMIN,
        ADMIN_ROLES,
        matrix_operations=("VIEW_INTERNALS",),
    ),
    _role_operation(
        "ADMIN_BACKLOG",
        "the Admin backlog reading (NFR-REL-06)",
        SUBJECT_ADMIN,
        ADMIN_ROLES,
        matrix_operations=("VIEW_INTERNALS",),
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
        SUBJECT_ADMIN, matrix_operations=("VIEW_INTERNALS",),
    ),
    _role_operation(
        "ADMIN_ATTEMPTS", "an Admin reads the processing-attempt ledger",
        SUBJECT_ADMIN, matrix_operations=("VIEW_INTERNALS",),
    ),
    _role_operation(
        "ADMIN_CONFIG", "an Admin reads the redacted configuration names",
        SUBJECT_ADMIN, matrix_operations=("VIEW_INTERNALS",),
    ),
    _role_operation(
        "ADMIN_EVALUATION_EXPORT", "an Admin exports labelled review data",
        SUBJECT_ADMIN, matrix_operations=("VIEW_INTERNALS",),
    ),
    _role_operation(
        "ADMIN_SYSTEM", "an Admin reads the two operator switches (owner D2)",
        SUBJECT_ADMIN, ADMIN_ROLES,
    ),
    _role_operation(
        "ADMIN_SYSTEM_PAUSE", "an Admin pauses processing or delivery (owner D2)",
        SUBJECT_ADMIN, ADMIN_ROLES,
    ),
    _role_operation(
        "ADMIN_SYSTEM_RESUME", "an Admin resumes processing or delivery (owner D2)",
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
    ("GET", "/dashboard/volunteer"): "DASHBOARD_VOLUNTEER",
    ("GET", "/dashboard/ah"): "DASHBOARD_AH",
    ("GET", "/dashboard/head"): "DASHBOARD_HEAD",
    ("GET", "/dashboard/admin"): "DASHBOARD_ADMIN",
    ("GET", "/admin/backlog"): "ADMIN_BACKLOG",
    ("GET", "/notifications"): "NOTIFICATION_LIST",
    ("POST", "/notifications/{notification_id}/read"): "NOTIFICATION_READ",
    ("POST", "/push-endpoints"): "PUSH_ENDPOINT_REGISTER",
    ("DELETE", "/push-endpoints/{endpoint_id}"): "PUSH_ENDPOINT_DEACTIVATE",
    ("GET", "/admin/denials"): "ADMIN_DENIALS",
    ("GET", "/admin/attempts"): "ADMIN_ATTEMPTS",
    ("GET", "/admin/config"): "ADMIN_CONFIG",
    ("GET", "/admin/evaluation/export"): "ADMIN_EVALUATION_EXPORT",
    ("GET", "/admin/system"): "ADMIN_SYSTEM",
    ("POST", "/admin/system/pause"): "ADMIN_SYSTEM_PAUSE",
    ("POST", "/admin/system/resume"): "ADMIN_SYSTEM_RESUME",
}


def authorize_proposal_acceptance(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    proposal_id: int,
) -> None:
    """Apply PUSH_ANNOUNCEMENT's strict Admin cell to an announcement accept.

    FR-AUT-04 gives Admin "—" on PUSH_ANNOUNCEMENT (owner decision A). The only
    surface for an announcement is ``POST /proposals/{id}/accept``, which is
    also the row-2 decision route where Admin has Override. This check runs
    after :func:`authorize_route` on the same connection: when the proposal is
    an announcement and the caller's *current* roster role is Admin, it records
    the denial against ``PUSH_ANNOUNCEMENT`` and raises. Every other caller and
    every other proposal type is left to the route table and the review
    service's own reviewer rule.
    """
    row = connection.execute(
        "SELECT ptype FROM proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None or str(row["ptype"]) != "ANNOUNCEMENT":
        return
    person = connection.execute(
        "SELECT role FROM person WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None or str(person["role"]).strip() != "Admin":
        return
    record_denial(
        connection,
        actor_person_id=person_id,
        operation="PUSH_ANNOUNCEMENT",
        subject_type=SUBJECT_PROPOSAL,
        subject_id=proposal_id,
        reason=REASON_NOT_ROLE,
    )
    raise RouteAuthorizationError(REASON_NOT_ROLE)


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
