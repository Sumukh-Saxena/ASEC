"""Shared authorization helper tests (Phase 7 S4; FR-AUT-06, A17).

The extraction moves ``review.py``'s denial recorder and active-actor check into
``backend/authorization.py`` without changing behaviour (plan §4.4). These tests
pin the two properties Phase 7's task writes depend on:

* a denial is committed in its own transaction, so it survives the rejected
  operation's rollback (FR-AUT-06, NFR-AUD-03);
* an unknown actor is recorded as ``NULL`` while an inactive actor keeps its id,
  because authorization always uses the current roster state (FR-AUT-05).

They also pin that ``review.py`` keeps thin aliases so the Phase 6 surface and
its exception type do not move.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from backend import review
from backend.authorization import (
    REASON_ACTOR_INACTIVE,
    REASON_ACTOR_NOT_FOUND,
    AuthorizationError,
    record_denial,
    require_active_actor,
)
from backend.db.connection import connect, transaction
from backend.db.migrations import apply_migrations

AH = 1
INACTIVE = 2


def _person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    role: str,
    *,
    active: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            person_id,
            name,
            f"+919811110{person_id:03d}",
            role,
            active,
            f"ROLL-{person_id:03d}",
        ),
    )
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, 1)",
        (person_id,),
    )


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    apply_migrations(connection)
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'PR')")
    _person(connection, AH, "Aarav", "AH")
    _person(connection, INACTIVE, "Dev", "Volunteer", active=0)
    yield connection
    connection.close()


def _denials(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM authorization_denial ORDER BY id").fetchall()


def test_record_denial_survives_the_rejected_operation_rollback(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-06 / NFR-AUD-03: the denial is committed outside the failed write.

    The rejected operation rolls back its own insert; the denial written after
    that rollback is still present.
    """
    try:
        with transaction(conn):
            conn.execute("INSERT INTO vertical (id, name) VALUES (2, 'Transport')")
            raise AuthorizationError("NOT_REVIEWER")
    except AuthorizationError:
        record_denial(
            conn,
            actor_person_id=AH,
            operation="PROPOSAL_REVIEW",
            subject_type="PROPOSAL",
            subject_id=7,
            reason="NOT_REVIEWER",
        )

    assert conn.execute("SELECT COUNT(*) FROM vertical WHERE id = 2").fetchone()[0] == 0
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == AH
    assert str(denial["operation"]) == "PROPOSAL_REVIEW"
    assert str(denial["subject_type"]) == "PROPOSAL"
    assert int(denial["subject_id"]) == 7
    assert str(denial["reason"]) == "NOT_REVIEWER"
    assert str(denial["attempted_at"]).strip() != ""


def test_record_denial_stores_an_unknown_actor_as_null(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-01/FR-AUT-06: an unknown caller is evidence with no roster link."""
    record_denial(
        conn,
        actor_person_id=999,
        operation="TASK_EDIT",
        subject_type="TASK",
        subject_id=None,
        reason="ACTOR_NOT_FOUND",
    )

    (denial,) = _denials(conn)
    assert denial["actor_person_id"] is None
    assert str(denial["reason"]) == "ACTOR_NOT_FOUND"


def test_require_active_actor_returns_the_current_row(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-02/FR-AUT-05: an active actor is returned and nothing is recorded."""
    actor = require_active_actor(
        conn,
        AH,
        operation="TASK_CLAIM",
        subject_type="TASK",
        subject_id=1,
    )

    assert int(actor["id"]) == AH
    assert str(actor["role"]) == "AH"
    assert int(actor["active"]) == 1
    assert _denials(conn) == []


def test_require_active_actor_records_a_missing_actor(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-06: a missing caller raises and is recorded with a NULL actor."""
    with pytest.raises(AuthorizationError) as excinfo:
        require_active_actor(
            conn,
            999,
            operation="TASK_CLAIM",
            subject_type="TASK",
            subject_id=1,
        )

    assert excinfo.value.reason == REASON_ACTOR_NOT_FOUND
    (denial,) = _denials(conn)
    assert denial["actor_person_id"] is None
    assert str(denial["reason"]) == REASON_ACTOR_NOT_FOUND
    assert str(denial["operation"]) == "TASK_CLAIM"
    assert int(denial["subject_id"]) == 1


def test_require_active_actor_records_an_inactive_actor(
    conn: sqlite3.Connection,
) -> None:
    """FR-AUT-05: a deactivated caller keeps its id in the denial evidence."""
    with pytest.raises(AuthorizationError) as excinfo:
        require_active_actor(
            conn,
            INACTIVE,
            operation="TASK_CLAIM",
            subject_type="TASK",
            subject_id=1,
        )

    assert excinfo.value.reason == REASON_ACTOR_INACTIVE
    (denial,) = _denials(conn)
    assert int(denial["actor_person_id"]) == INACTIVE
    assert str(denial["reason"]) == REASON_ACTOR_INACTIVE


def test_review_module_keeps_the_shared_recorder_alias() -> None:
    """Plan §4.4: ``review._record_denial`` stays the same callable."""
    assert review._record_denial is record_denial


def test_review_module_keeps_its_authorization_error_type(
    conn: sqlite3.Connection,
) -> None:
    """Phase 6 callers still catch ``ReviewAuthorizationError`` with the reason."""
    with pytest.raises(review.ReviewAuthorizationError) as excinfo:
        review._require_active_actor(
            conn,
            999,
            operation="PROPOSAL_REVIEW",
            subject_type="PROPOSAL",
            subject_id=None,
        )

    assert str(excinfo.value) == REASON_ACTOR_NOT_FOUND
    (denial,) = _denials(conn)
    assert str(denial["reason"]) == REASON_ACTOR_NOT_FOUND
