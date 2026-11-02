"""Shared authorization helpers for every service (Phase 7 S4).

Moved verbatim from ``backend/review.py`` (plan §4.4, no new semantics) so the
task lifecycle and later phases record denials through one implementation
instead of one per module (FR-AUT-06, NFR-AUD-03).

Boundary rules:

* Authorization always uses the current roster state, so a deactivated person is
  rejected even if they were active when the record was created (FR-AUT-05).
* A denial is written in its own committed transaction, so it survives the
  rejected operation's rollback (FR-AUT-06). Call :func:`record_denial` after
  the failed write transaction has rolled back and before re-raising.
* An unknown caller has no roster row to reference, so the denial stores a
  ``NULL`` actor; a known-but-inactive caller keeps its id.

This module never imports a service module and never raises a service-specific
exception type; callers adapt :class:`AuthorizationError` to their own type.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from backend.db.connection import transaction

REASON_ACTOR_NOT_FOUND = "ACTOR_NOT_FOUND"
REASON_ACTOR_INACTIVE = "ACTOR_INACTIVE"


class AuthorizationError(Exception):
    """The caller is unknown, inactive, or not permitted for the operation.

    ``reason`` is the stable token recorded in ``authorization_denial``.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _person_row(
    connection: sqlite3.Connection, person_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, name, role, active FROM person WHERE id = ?", (person_id,)
    ).fetchone()


def record_denial(
    connection: sqlite3.Connection,
    *,
    actor_person_id: int | None,
    operation: str,
    subject_type: str,
    subject_id: int | None,
    reason: str,
) -> None:
    """Record FR-AUT-06's "recorded" half in its own committed transaction.

    Called before raising the caller's authorization error, outside any outer
    transaction, so the denial survives the rejected operation's rollback.
    """
    known_actor: int | None = None
    if actor_person_id is not None:
        row = connection.execute(
            "SELECT id FROM person WHERE id = ?", (actor_person_id,)
        ).fetchone()
        if row is not None:
            known_actor = int(row["id"])
    with transaction(connection):
        connection.execute(
            "INSERT INTO authorization_denial (actor_person_id, operation,"
            " subject_type, subject_id, reason, attempted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                known_actor,
                operation,
                subject_type,
                subject_id,
                reason,
                _utc_now(),
            ),
        )


def require_active_actor(
    connection: sqlite3.Connection,
    actor_person_id: int,
    *,
    operation: str,
    subject_type: str,
    subject_id: int | None,
) -> sqlite3.Row:
    """Authenticate the caller against the roster before any operation.

    A missing or inactive actor is recorded in ``authorization_denial``
    (its own transaction) before :class:`AuthorizationError` is raised.
    """
    actor = _person_row(connection, actor_person_id)
    if actor is None:
        record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=operation,
            subject_type=subject_type,
            subject_id=subject_id,
            reason=REASON_ACTOR_NOT_FOUND,
        )
        raise AuthorizationError(REASON_ACTOR_NOT_FOUND)
    if int(actor["active"]) != 1:
        record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=operation,
            subject_type=subject_type,
            subject_id=subject_id,
            reason=REASON_ACTOR_INACTIVE,
        )
        raise AuthorizationError(REASON_ACTOR_INACTIVE)
    return actor
