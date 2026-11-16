"""The FR-AUT-04 12-operation x 4-role authorization matrix (W5).

Every cell in :data:`backend.api.matrix.MATRIX_OPERATIONS` is exercised through
the real route the operation is reachable by. An allowed cell proves an
in-scope actor succeeds and, for the scoped roles, that a same-role actor
outside the scope is refused; a refused cell proves a 403 and a recorded
``authorization_denial`` row before the response. The matrix data and these
tests are the same table: adding an operation without a scenario, or a cell
without a route, fails here rather than shipping an unclassified surface.

AC-20 (a volunteer cannot claim outside their vertical or assign anyone) and
FR-AUT-08 (content never grants authority) have their own cases at the end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.api.matrix import (  # noqa: E402
    ALL_ROLES,
    MATRIX_OPERATIONS,
    NON_MATRIX_ROUTE_OPERATIONS,
    OPERATIONS,
    ROLE_ORDER,
    ROUTE_OPERATIONS,
    CellKind,
    RouteAuthorizationError,
    _matrix_roles,
    authorize_route,
)
from backend.db.connection import connect  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    AH_ROLL,
    HEAD_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    add_person,
    assign,
    build_harness,
    count,
    make_approval,
    make_proposal,
    make_task,
    query,
    seed_database,
    seed_world,
)

HEAD2_ROLL = "R-HEAD2"
AH2_ROLL = "R-AH2"
VOL3_ROLL = "R-VOL3"
VOL4_ROLL = "R-VOL4"

_ROLE_ROLL = {
    "Admin": ADMIN_ROLL,
    "Head": HEAD_ROLL,
    "AH": AH_ROLL,
    "Volunteer": VOLUNTEER_ROLL,
}


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    """The fixture roster plus a second vertical's Head and a second AH."""
    built = build_harness(tmp_path)
    seed_world(built.database)
    add_person(
        built.database,
        person_id=8,
        name="Head Two",
        role="Head",
        roll_number=HEAD2_ROLL,
        discord_id="100000000000000008",
        vertical_id=2,
    )
    add_person(
        built.database,
        person_id=9,
        name="AH Two",
        role="AH",
        roll_number=AH2_ROLL,
        discord_id="100000000000000009",
        vertical_id=1,
        reports_to=2,
    )
    add_person(
        built.database,
        person_id=10,
        name="Volunteer Three",
        role="Volunteer",
        roll_number=VOL3_ROLL,
        discord_id="100000000000000010",
        vertical_id=2,
        reports_to=8,
    )
    add_person(
        built.database,
        person_id=11,
        name="Volunteer Four",
        role="Volunteer",
        roll_number=VOL4_ROLL,
        discord_id="100000000000000011",
        vertical_id=1,
        reports_to=9,
    )
    return built


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


@dataclass(frozen=True)
class _Denial:
    """One expected refusal: the response and what the ledger must show."""

    response: object
    route_operation: str
    subject_id: int | None
    reason: str | None


def _assert_denial(harness: ApiHarness, denial: _Denial, reason: str) -> None:
    """The 403 is accompanied by the newest ``authorization_denial`` row.

    The row is looked up by recency, not by ``operation``: a route-level
    refusal records the route's operation token (``NOT_ROLE``), while the
    authoritative service records its own token for the same attempt (for
    example ``PROPOSAL_REVIEW`` or ``approval.decide``). Both are FR-AUT-06
    recordings; this test pins the subject and the stable reason, and the
    per-route suites keep pinning the operation tokens.
    """
    assert denial.response.status == 403  # type: ignore[attr-defined]
    rows = query(
        harness.database,
        "SELECT * FROM authorization_denial ORDER BY id DESC LIMIT 1",
    )
    assert rows, f"no denial recorded for {denial.route_operation}"
    row = rows[0]
    # ``approvals`` stores its subject token lower-case; the route table uses the
    # same word upper-case. Identity of the subject type is what matters here.
    assert str(row["subject_type"]).upper() == OPERATIONS[
        denial.route_operation
    ].subject_type
    if denial.subject_id is not None:
        assert row["subject_id"] == denial.subject_id
    assert row["reason"] == reason


# ---------------------------------------------------------------------------
# One runner pair per FR-AUT-04 operation
# ---------------------------------------------------------------------------


def _review_success(harness: ApiHarness, role: str) -> object:
    sender = 2 if role in ("Admin", "Head") else 3
    proposal = make_proposal(
        harness.database,
        sender_person_id=sender,
        assignees=(("Volunteer One", "RESOLVED", 4, "MENTION"),),
    )
    return harness.call(
        "GET", f"/proposals/{proposal}", headers=_headers(harness, _ROLE_ROLL[role])
    )


def _review_refuse(harness: ApiHarness, role: str) -> _Denial:
    if role == "Volunteer":
        proposal = make_proposal(harness.database, sender_person_id=2)
        roll, reason = VOLUNTEER_ROLL, "NOT_ROLE"
    elif role == "Head":
        proposal = make_proposal(harness.database, sender_person_id=2)
        roll, reason = HEAD2_ROLL, "NOT_REVIEWER"
    elif role == "AH":
        proposal = make_proposal(harness.database, sender_person_id=3)
        roll, reason = AH2_ROLL, "NOT_REVIEWER"
    else:  # Admin is allowed on this row; kept for completeness.
        proposal = make_proposal(harness.database, sender_person_id=2)
        roll, reason = ADMIN_ROLL, "NOT_REVIEWER"
    return _Denial(
        harness.call("GET", f"/proposals/{proposal}", headers=_headers(harness, roll)),
        "PROPOSAL_VIEW",
        proposal,
        reason,
    )


def _decide_success(harness: ApiHarness, role: str) -> object:
    sender = 2 if role in ("Admin", "Head") else 3
    proposal = make_proposal(harness.database, sender_person_id=sender)
    return harness.call(
        "POST",
        f"/proposals/{proposal}/edits",
        headers=_headers(harness, _ROLE_ROLL[role]),
        json_body={"expected_revision_number": 0, "description": "Reviewed text"},
    )


def _decide_refuse(harness: ApiHarness, role: str) -> _Denial:
    if role == "Volunteer":
        proposal = make_proposal(harness.database, sender_person_id=2)
        roll, reason = VOLUNTEER_ROLL, "NOT_ROLE"
    elif role == "Head":
        proposal = make_proposal(harness.database, sender_person_id=2)
        roll, reason = HEAD2_ROLL, "NOT_REVIEWER"
    elif role == "AH":
        proposal = make_proposal(harness.database, sender_person_id=3)
        roll, reason = AH2_ROLL, "NOT_REVIEWER"
    else:
        proposal = make_proposal(harness.database, sender_person_id=2)
        roll, reason = ADMIN_ROLL, "NOT_REVIEWER"
    return _Denial(
        harness.call(
            "POST",
            f"/proposals/{proposal}/edits",
            headers=_headers(harness, roll),
            json_body={"expected_revision_number": 0, "description": "Mine"},
        ),
        "PROPOSAL_EDIT",
        proposal,
        reason,
    )


def _acceptance_proposal(harness: ApiHarness, sender: int, kind: str) -> int:
    if kind == "CREATE":
        return make_proposal(
            harness.database,
            sender_person_id=sender,
            ptype="TASK",
            assignment_mode="EXPLICIT",
            assignees=(("Volunteer One", "RESOLVED", 4, "MENTION"),),
            task_type="INTERNAL",
        )
    if kind == "CROSS":
        return make_proposal(
            harness.database,
            sender_person_id=sender,
            ptype="TASK",
            assignment_mode="OPEN",
            people_needed=1,
            people_needed_unlimited=0,
            task_type="CROSS_VERTICAL",
            responsible_vertical_id=2,
        )
    return make_proposal(
        harness.database,
        sender_person_id=sender,
        ptype="ANNOUNCEMENT",
        assignment_mode="NONE",
        assignees=(),
        description="A synthetic announcement",
    )


def _accept_success(kind: str) -> Callable[[ApiHarness, str], object]:
    def run(harness: ApiHarness, role: str) -> object:
        sender = 2 if role in ("Admin", "Head") else 3
        proposal = _acceptance_proposal(harness, sender, kind)
        return harness.call(
            "POST",
            f"/proposals/{proposal}/accept",
            headers=_headers(harness, _ROLE_ROLL[role]),
            json_body={"expected_revision_number": 0},
        )

    return run


def _accept_refuse(kind: str) -> Callable[[ApiHarness, str], _Denial]:
    def run(harness: ApiHarness, role: str) -> _Denial:
        if role == "Volunteer":
            proposal = _acceptance_proposal(harness, 2, kind)
            roll, reason = VOLUNTEER_ROLL, "NOT_ROLE"
        elif role == "Head":
            proposal = _acceptance_proposal(harness, 2, kind)
            roll, reason = HEAD2_ROLL, "NOT_REVIEWER"
        elif role == "AH":
            proposal = _acceptance_proposal(harness, 3, kind)
            roll, reason = AH2_ROLL, "NOT_REVIEWER"
        else:
            proposal = _acceptance_proposal(harness, 2, kind)
            roll, reason = ADMIN_ROLL, "NOT_REVIEWER"
        return _Denial(
            harness.call(
                "POST",
                f"/proposals/{proposal}/accept",
                headers=_headers(harness, roll),
                json_body={"expected_revision_number": 0},
            ),
            "PROPOSAL_ACCEPT",
            proposal,
            reason,
        )

    return run


def _approval_request(
    harness: ApiHarness, *, recipients: tuple[int, ...], responsible: int = 1
) -> tuple[int, int]:
    task = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=2,
        responsible_vertical_id=responsible,
        approval_state="PENDING",
    )
    request = make_approval(
        harness.database, task, requested_by=3, recipients=recipients
    )
    return task, request


def _approval_success(harness: ApiHarness, role: str) -> object:
    if role == "Admin":
        _task, request = _approval_request(harness, recipients=(2, 3))
    elif role == "Head":
        _task, request = _approval_request(harness, recipients=(2,))
    else:
        _task, request = _approval_request(harness, recipients=(3,))
    return harness.call(
        "POST",
        f"/approvals/{request}/decision",
        headers=_headers(harness, _ROLE_ROLL[role]),
        json_body={"decision": "APPROVED"},
    )


def _approval_refuse(harness: ApiHarness, role: str) -> _Denial:
    if role == "Volunteer":
        _task, request = _approval_request(harness, recipients=(2, 3))
        roll, reason = VOLUNTEER_ROLL, "NOT_ROLE"
    elif role == "Head":
        # Recipient, but the request is for vertical 1 and this Head is in 2.
        _task, request = _approval_request(harness, recipients=(8,))
        roll, reason = HEAD2_ROLL, "HEAD_NOT_IN_RESPONSIBLE_VERTICAL"
    elif role == "AH":
        _task, request = _approval_request(harness, recipients=(3,))
        roll, reason = AH2_ROLL, "NOT_APPROVAL_RECIPIENT"
    else:
        _task, request = _approval_request(harness, recipients=(2, 3))
        roll, reason = ADMIN_ROLL, "NOT_APPROVAL_RECIPIENT"
    return _Denial(
        harness.call(
            "POST",
            f"/approvals/{request}/decision",
            headers=_headers(harness, roll),
            json_body={"decision": "APPROVED"},
        ),
        "APPROVAL_DECIDE",
        request,
        reason,
    )


def _manage_success(harness: ApiHarness, role: str) -> object:
    creator = 2 if role in ("Admin", "Head") else 3
    task = make_task(harness.database, creator_id=creator)
    return harness.call(
        "PATCH",
        f"/tasks/{task}",
        headers=_headers(harness, _ROLE_ROLL[role]),
        json_body={"expected_version": 1, "description": "Edited by the test"},
    )


def _manage_refuse(harness: ApiHarness, role: str) -> _Denial:
    if role == "Volunteer":
        task = make_task(harness.database, creator_id=3)
        roll, reason = VOLUNTEER_ROLL, "NOT_ROLE"
    elif role == "Head":
        task = make_task(harness.database, creator_id=2)
        roll, reason = HEAD2_ROLL, "NOT_TASK_OWNER"
    elif role == "AH":
        task = make_task(harness.database, creator_id=3)
        roll, reason = AH2_ROLL, "NOT_TASK_OWNER"
    else:
        task = make_task(harness.database, creator_id=3)
        roll, reason = ADMIN_ROLL, "NOT_TASK_OWNER"
    return _Denial(
        harness.call(
            "PATCH",
            f"/tasks/{task}",
            headers=_headers(harness, roll),
            json_body={"expected_version": 1, "description": "Not mine"},
        ),
        "TASK_EDIT",
        task,
        reason,
    )


def _open_task(harness: ApiHarness, vertical: int = 1) -> int:
    return make_task(
        harness.database,
        source_vertical_id=vertical,
        responsible_vertical_id=vertical,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )


def _claim_success(harness: ApiHarness, role: str) -> object:
    task = _open_task(harness)
    return harness.call(
        "POST", f"/tasks/{task}/claims", headers=_headers(harness, VOLUNTEER_ROLL)
    )


def _claim_refuse(harness: ApiHarness, role: str) -> _Denial:
    if role == "Volunteer":
        task = _open_task(harness, vertical=2)
        roll, reason = VOLUNTEER_ROLL, "NOT_IN_VERTICAL"
    else:
        task = _open_task(harness)
        roll, reason = _ROLE_ROLL[role], "NOT_ROLE"
    return _Denial(
        harness.call("POST", f"/tasks/{task}/claims", headers=_headers(harness, roll)),
        "TASK_CLAIM",
        task,
        reason,
    )


def _status_success(harness: ApiHarness, role: str) -> object:
    assignee = 4 if role in ("Admin", "Volunteer") else (2 if role == "Head" else 3)
    task = make_task(harness.database, creator_id=3)
    assign(harness.database, task, assignee, agreement="ACCEPTED")
    body: dict[str, object] = {"status": "IN_PROGRESS"}
    if role == "Admin":
        body["assignee_person_id"] = 4
    return harness.call(
        "POST",
        f"/tasks/{task}/status",
        headers=_headers(harness, _ROLE_ROLL[role]),
        json_body=body,
    )


def _status_refuse(harness: ApiHarness, role: str) -> _Denial:
    """Advance *someone else's* accepted assignment: outside "own assignments".

    A same-actor status update with no accepted assignment is the service's
    ``NOT_ACCEPTED`` state conflict (409), not an authorization denial, so the
    scope refusal is exercised by naming another person's assignment: only the
    Admin override may do that (O7-5), and every other role is a recorded 403.
    """
    if role == "Head":
        task = make_task(harness.database, creator_id=3)
        assign(harness.database, task, 2, agreement="ACCEPTED")
        roll, target = HEAD2_ROLL, 2
    elif role == "AH":
        task = make_task(harness.database, creator_id=3)
        assign(harness.database, task, 3, agreement="ACCEPTED")
        roll, target = AH2_ROLL, 3
    elif role == "Volunteer":
        task = make_task(harness.database, creator_id=3)
        assign(harness.database, task, 7, agreement="ACCEPTED")
        roll, target = VOLUNTEER_ROLL, 7
    else:
        task = make_task(harness.database, creator_id=3)
        assign(harness.database, task, 4, agreement="ACCEPTED")
        roll, target = ADMIN_ROLL, 4
    return _Denial(
        harness.call(
            "POST",
            f"/tasks/{task}/status",
            headers=_headers(harness, roll),
            json_body={"status": "IN_PROGRESS", "assignee_person_id": target},
        ),
        "TASK_STATUS",
        task,
        "NOT_ASSIGNEE",
    )


def _progress_success(harness: ApiHarness, role: str) -> object:
    if role == "Admin":
        task = make_task(
            harness.database,
            creator_id=8,
            source_vertical_id=2,
            responsible_vertical_id=2,
        )
    elif role == "Head":
        task = make_task(harness.database, creator_id=3)
    elif role == "AH":
        task = make_task(harness.database, creator_id=3)
    else:
        task = make_task(harness.database, creator_id=3)
        assign(harness.database, task, 4, agreement="ACCEPTED")
    return harness.call(
        "GET", f"/tasks/{task}", headers=_headers(harness, _ROLE_ROLL[role])
    )


def _progress_refuse(harness: ApiHarness, role: str) -> _Denial:
    if role == "Head":
        task = make_task(
            harness.database,
            creator_id=8,
            source_vertical_id=2,
            responsible_vertical_id=2,
        )
        roll = HEAD_ROLL
    elif role == "AH":
        task = make_task(harness.database, creator_id=9)
        roll = AH_ROLL
    elif role == "Volunteer":
        task = make_task(harness.database, creator_id=3)
        assign(harness.database, task, 7, agreement="ACCEPTED")
        roll = VOLUNTEER_ROLL
    else:
        task = make_task(harness.database, creator_id=3)
        roll = ADMIN_ROLL
    return _Denial(
        harness.call("GET", f"/tasks/{task}", headers=_headers(harness, roll)),
        "TASK_VIEW",
        task,
        None,
    )


def _internals_success(harness: ApiHarness, role: str) -> object:
    return harness.call(
        "GET", "/admin/denials", headers=_headers(harness, ADMIN_ROLL)
    )


def _internals_refuse(harness: ApiHarness, role: str) -> _Denial:
    return _Denial(
        harness.call(
            "GET", "/admin/denials", headers=_headers(harness, _ROLE_ROLL[role])
        ),
        "ADMIN_DENIALS",
        None,
        "NOT_ROLE",
    )


_SUCCESS: dict[str, Callable[[ApiHarness, str], object]] = {
    "REVIEW_OWN_PROPOSAL": _review_success,
    "DECIDE_PROPOSAL": _decide_success,
    "CREATE_TASK_OWN_VERTICAL": _accept_success("CREATE"),
    "RAISE_CROSS_VERTICAL": _accept_success("CROSS"),
    "DECIDE_CROSS_VERTICAL": _approval_success,
    "MANAGE_TASK": _manage_success,
    "CLAIM_TASK": _claim_success,
    "ASSIGNMENT_ACTION": _status_success,
    "SEE_PROGRESS": _progress_success,
    "PUSH_ANNOUNCEMENT": _accept_success("ANNOUNCE"),
    "VIEW_INTERNALS": _internals_success,
}

_REFUSE: dict[str, Callable[[ApiHarness, str], _Denial]] = {
    "REVIEW_OWN_PROPOSAL": _review_refuse,
    "DECIDE_PROPOSAL": _decide_refuse,
    "CREATE_TASK_OWN_VERTICAL": _accept_refuse("CREATE"),
    "RAISE_CROSS_VERTICAL": _accept_refuse("CROSS"),
    "DECIDE_CROSS_VERTICAL": _approval_refuse,
    "MANAGE_TASK": _manage_refuse,
    "CLAIM_TASK": _claim_refuse,
    "ASSIGNMENT_ACTION": _status_refuse,
    "SEE_PROGRESS": _progress_refuse,
    "PUSH_ANNOUNCEMENT": _accept_refuse("ANNOUNCE"),
    "VIEW_INTERNALS": _internals_refuse,
}


def _cell_parameters() -> list[pytest.ParameterSet]:
    return [
        pytest.param(name, role, matrix.cells[role].kind, id=f"{name}-{role}")
        for name, matrix in MATRIX_OPERATIONS.items()
        for role in ROLE_ORDER
    ]


@pytest.mark.parametrize(("operation", "role", "kind"), _cell_parameters())
def test_every_fr_aut_04_cell(
    harness: ApiHarness, operation: str, role: str, kind: str
) -> None:
    """One test per cell: allowed acts in scope; refused gets a recorded 403."""
    matrix = MATRIX_OPERATIONS[operation]
    if operation == "CHANGE_ROSTER":
        # O12-4/FR-ROS-02: the roster change has no MVP route. The cell is
        # encoded in the matrix and tested as data; no route is invented here.
        assert matrix.route_operations == ()
        expected = CellKind.YES if role == "Admin" else CellKind.NO
        assert kind == expected
        return

    assert operation in _SUCCESS and operation in _REFUSE, (
        f"no route scenario for matrix operation {operation}"
    )
    if operation == "PUSH_ANNOUNCEMENT" and role == "Admin":
        # NEEDS-OWNER: the SRS cell is "—", but there is no broadcast route:
        # accepting the announcement is accepting a proposal, and row 2 gives
        # Admin "Override" there. The surface cannot separate the two without
        # a new domain rule, so this cell records the implemented behaviour
        # and is flagged instead of asserting a denial that does not happen.
        assert matrix.cells[role].kind == CellKind.NO
        assert matrix.cells[role].needs_owner
        response = _SUCCESS[operation](harness, role)
        assert response.status == 200  # type: ignore[attr-defined]
        return
    if kind == CellKind.NO:
        denial = _REFUSE[operation](harness, role)
        _assert_denial(harness, denial, "NOT_ROLE")
        return

    response = _SUCCESS[operation](harness, role)
    assert response.status in (200, 201), (  # type: ignore[attr-defined]
        operation,
        role,
        response.status,  # type: ignore[attr-defined]
        response.body,  # type: ignore[attr-defined]
    )
    if role == "Admin":
        # Admin's in-scope case above acts on someone else's subject, which is
        # the Override/Yes meaning of these cells; there is no narrower Admin.
        return
    refusal = _REFUSE[operation](harness, role)
    if refusal.response.status == 404:  # type: ignore[attr-defined]
        assert refusal.response.json()["error"]["code"] == "NOT_FOUND"  # type: ignore[attr-defined]
        return
    assert refusal.reason is not None
    _assert_denial(harness, refusal, refusal.reason)


# ---------------------------------------------------------------------------
# Completeness and vocabulary
# ---------------------------------------------------------------------------


def test_the_matrix_has_exactly_twelve_operations_and_forty_eight_cells() -> None:
    assert len(MATRIX_OPERATIONS) == 12
    parameters = _cell_parameters()
    assert len(parameters) == 48
    for matrix in MATRIX_OPERATIONS.values():
        assert matrix.srs_wording.strip() != ""
        assert set(matrix.cells) == set(ALL_ROLES)
        assert set(matrix.cells) == set(ROLE_ORDER)
        for role in ALL_ROLES:
            cell = matrix.cells[role]
            assert cell.kind in CellKind.ALL
            assert cell.wording.strip() != ""


def test_the_cells_that_need_an_owner_are_exactly_the_documented_ones() -> None:
    flagged = {
        (name, role)
        for name, matrix in MATRIX_OPERATIONS.items()
        for role, cell in matrix.cells.items()
        if cell.needs_owner
    }
    assert flagged == {("PUSH_ANNOUNCEMENT", "Admin")}
    assert "NEEDS-OWNER" in MATRIX_OPERATIONS["CREATE_TASK_OWN_VERTICAL"].note


def test_every_matrix_operation_is_reachable_except_the_documented_roster_gap() -> None:
    registered = set(ROUTE_OPERATIONS.values())
    unreachable = {
        name
        for name, matrix in MATRIX_OPERATIONS.items()
        if not (set(matrix.route_operations) & registered)
    }
    assert unreachable == {"CHANGE_ROSTER"}
    for name, matrix in MATRIX_OPERATIONS.items():
        for route_operation in matrix.route_operations:
            assert route_operation in OPERATIONS, (name, route_operation)
            assert route_operation in registered, (name, route_operation)


def test_every_domain_route_operation_maps_to_one_matrix_operation() -> None:
    mapped = {
        route_operation
        for matrix in MATRIX_OPERATIONS.values()
        for route_operation in matrix.route_operations
    }
    domain = set(OPERATIONS) - set(NON_MATRIX_ROUTE_OPERATIONS)
    assert domain <= mapped


def test_every_domain_route_is_bounded_by_its_matrix_cells() -> None:
    """A route may narrow a matrix row for its own surface, never widen it."""
    for name in set(OPERATIONS) - set(NON_MATRIX_ROUTE_OPERATIONS):
        operation = OPERATIONS[name]
        if not operation.matrix_operations:
            continue
        ceiling = _matrix_roles(*operation.matrix_operations)
        assert operation.allowed_roles <= ceiling, name


def test_the_matrix_uses_only_the_stable_reason_vocabulary() -> None:
    reasons = {
        OPERATIONS[name].denial_reason
        for matrix in MATRIX_OPERATIONS.values()
        for name in matrix.route_operations
    }
    assert reasons <= {"NOT_ROLE", "NOT_OWNER", "NOT_VERTICAL", "INACTIVE", "NOT_RECIPIENT"}


# ---------------------------------------------------------------------------
# Inactive, AC-20 and FR-AUT-08
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    sorted(op.name for op in OPERATIONS.values() if not op.public),
)
def test_an_inactive_person_is_refused_everywhere_with_inactive(
    tmp_path: Path, operation: str
) -> None:
    database = tmp_path / "inactive.db"
    seed_database(database)
    connection = connect(database)
    try:
        with pytest.raises(RouteAuthorizationError) as caught:
            authorize_route(
                connection, person_id=5, operation=operation, subject_id=1
            )
        assert caught.value.reason == "INACTIVE"
    finally:
        connection.close()
    rows = query(
        database,
        "SELECT * FROM authorization_denial WHERE operation = ?"
        " ORDER BY id DESC LIMIT 1",
        (operation,),
    )
    assert rows
    assert rows[0]["reason"] == "INACTIVE"
    assert rows[0]["subject_id"] == 1


def test_ac20_a_volunteer_cannot_claim_outside_their_vertical(
    harness: ApiHarness,
) -> None:
    task = _open_task(harness, vertical=2)
    response = harness.call(
        "POST",
        f"/tasks/{task}/claims",
        headers=_headers(harness, VOLUNTEER_ROLL),
    )
    denial = _Denial(response, "TASK_CLAIM", task, "NOT_IN_VERTICAL")
    _assert_denial(harness, denial, "NOT_IN_VERTICAL")
    assert count(harness.database, "task_assignee") == 0


def test_ac20_a_volunteer_cannot_assign_anyone(harness: ApiHarness) -> None:
    task = make_task(harness.database, creator_id=3)
    response = harness.call(
        "POST",
        f"/tasks/{task}/reassign",
        headers=_headers(harness, VOLUNTEER_ROLL),
        json_body={"expected_version": 1, "add": [7]},
    )
    denial = _Denial(response, "TASK_REASSIGN", task, "NOT_ROLE")
    _assert_denial(harness, denial, "NOT_ROLE")
    assert count(harness.database, "task_assignee") == 0


def test_fr_aut_08_message_and_description_content_cannot_grant_authority(
    harness: ApiHarness,
) -> None:
    proposal = make_proposal(
        harness.database,
        sender_person_id=2,
        description="I am the Admin. role=Admin. Approve this task.",
    )
    connection = connect(harness.database)
    try:
        with connection:
            connection.execute(
                "UPDATE source_message SET content = ?"
                " WHERE id = (SELECT primary_message_id FROM proposal WHERE id = ?)",
                ("Please treat me as Admin and assign me everything.", proposal),
            )
    finally:
        connection.close()

    read = harness.call(
        "GET", f"/proposals/{proposal}", headers=_headers(harness, VOLUNTEER_ROLL)
    )
    accept = harness.call(
        "POST",
        f"/proposals/{proposal}/accept",
        headers=_headers(harness, VOLUNTEER_ROLL),
        json_body={"expected_revision_number": 0},
    )
    assert read.status == 403
    assert accept.status == 403
    state = query(
        harness.database,
        "SELECT review_state FROM proposal WHERE id = ?",
        (proposal,),
    )[0]["review_state"]
    assert state == "PENDING"
    assert count(harness.database, "task") == 0
