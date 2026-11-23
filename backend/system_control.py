"""The two operator pause switches (Phase 12 D2, owner decision C).

``PROCESSING`` stops the worker's next pipeline tick; ``DELIVERY`` stops the
next delivery pass. They are independent and neither stops ingestion, which
runs before the pipeline tick in :func:`backend.worker.run_live_tick`.

The table holds current state only (one row per control); a missing row is
"resumed", so a fresh database reads both switches as off without seed data.
Every write is one upsert, which makes a repeated pause or resume idempotent.

Authorization is enforced here as well as at the route gate: the service
re-reads the current roster role and records its own denial (FR-AUT-01/06).
Nothing here reads configuration, calls AI services or touches messages.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backend.authorization import (
    AuthorizationError,
    record_denial,
    require_active_actor,
)
from backend.db.connection import transaction

#: The two switches; the migration's CHECK accepts exactly these tokens.
PROCESSING = "PROCESSING"
DELIVERY = "DELIVERY"
CONTROLS: tuple[str, ...] = (PROCESSING, DELIVERY)

_OPERATION_PAUSE = "SYSTEM_PAUSE"
_OPERATION_RESUME = "SYSTEM_RESUME"
_SUBJECT_ADMIN = "ADMIN"
_REASON_NOT_ROLE = "NOT_ROLE"


class SystemControlError(Exception):
    """The switch request is invalid for the current state."""


class InvalidControlError(SystemControlError):
    """The named control is not one of :data:`CONTROLS`."""


class SystemControlAuthorizationError(AuthorizationError, SystemControlError):
    """The caller is not an active Admin (the route gate also refuses)."""


@dataclass(frozen=True)
class SystemState:
    """The two switches as one read-only snapshot."""

    processing_paused: bool
    delivery_paused: bool


def _require_control(control: str) -> str:
    if control not in CONTROLS:
        raise InvalidControlError(f"unknown system control {control!r}")
    return control


def _require_admin(
    connection: sqlite3.Connection,
    *,
    actor_person_id: int,
    operation: str,
) -> sqlite3.Row:
    actor = require_active_actor(
        connection,
        actor_person_id,
        operation=operation,
        subject_type=_SUBJECT_ADMIN,
        subject_id=None,
    )
    if str(actor["role"]).strip() != "Admin":
        record_denial(
            connection,
            actor_person_id=actor_person_id,
            operation=operation,
            subject_type=_SUBJECT_ADMIN,
            subject_id=None,
            reason=_REASON_NOT_ROLE,
        )
        raise SystemControlAuthorizationError(_REASON_NOT_ROLE)
    return actor


def _paused(connection: sqlite3.Connection, control: str) -> bool:
    row = connection.execute(
        "SELECT paused FROM system_control WHERE name = ?", (control,)
    ).fetchone()
    return False if row is None else int(row["paused"]) == 1


def read_state(connection: sqlite3.Connection) -> SystemState:
    """Both switches; an absent row is resumed (the migration seeds none)."""
    return SystemState(
        processing_paused=_paused(connection, PROCESSING),
        delivery_paused=_paused(connection, DELIVERY),
    )


def is_processing_paused(connection: sqlite3.Connection) -> bool:
    """The worker's read-only check before a pipeline tick."""
    return _paused(connection, PROCESSING)


def is_delivery_paused(connection: sqlite3.Connection) -> bool:
    """The live tick's read-only check before the delivery hook."""
    return _paused(connection, DELIVERY)


def _set(
    connection: sqlite3.Connection,
    *,
    control: str,
    paused: bool,
    actor_person_id: int,
    now: str,
    operation: str,
) -> SystemState:
    _require_control(control)
    _require_admin(
        connection, actor_person_id=actor_person_id, operation=operation
    )
    with transaction(connection):
        connection.execute(
            "INSERT INTO system_control (name, paused, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET paused = excluded.paused,"
            " updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (control, 1 if paused else 0, now, actor_person_id),
        )
    return read_state(connection)


def pause(
    connection: sqlite3.Connection,
    *,
    control: str,
    actor_person_id: int,
    now: str,
) -> SystemState:
    """Pause one control and return both switches after the write."""
    return _set(
        connection,
        control=control,
        paused=True,
        actor_person_id=actor_person_id,
        now=now,
        operation=_OPERATION_PAUSE,
    )


def resume(
    connection: sqlite3.Connection,
    *,
    control: str,
    actor_person_id: int,
    now: str,
) -> SystemState:
    """Resume one control and return both switches after the write."""
    return _set(
        connection,
        control=control,
        paused=False,
        actor_person_id=actor_person_id,
        now=now,
        operation=_OPERATION_RESUME,
    )
