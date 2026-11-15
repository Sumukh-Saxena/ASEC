"""Admin read models (W4; plan §4.3, FR-UI-05, NFR-REL-06).

These are the only statements behind the Admin read routes. Each one is a
bounded, newest-first page over an existing ledger table, and each one selects
an explicit column list: the raw ``processing_attempt.error`` text is reduced to
``has_error`` because that column may carry a provider string and this API never
returns free-text diagnostics (NFR-SEC-01, EIR-02).

Read-only by construction: no function here writes, opens a transaction or
returns a domain object with behaviour.
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: The ledger rows a page of denials may contain.
_DENIAL_COLUMNS = (
    "id, actor_person_id, operation, subject_type, subject_id, reason,"
    " attempted_at"
)

#: The attempt columns that are safe to expose; ``error`` is not one of them.
_ATTEMPT_COLUMNS = (
    "id, stage, bucket_id, model_id, model_version, config_version,"
    " started_at, finished_at, outcome, error"
)


def denials_page(
    connection: sqlite3.Connection, *, limit: int, offset: int = 0
) -> tuple[dict[str, Any], ...]:
    """The newest recorded denials, newest first, as plain dicts."""
    rows = connection.execute(
        f"SELECT {_DENIAL_COLUMNS} FROM authorization_denial"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return tuple(
        {
            "id": int(row["id"]),
            "actor_person_id": (
                None if row["actor_person_id"] is None else int(row["actor_person_id"])
            ),
            "operation": str(row["operation"]),
            "subject_type": str(row["subject_type"]),
            "subject_id": None if row["subject_id"] is None else int(row["subject_id"]),
            "reason": str(row["reason"]),
            "attempted_at": str(row["attempted_at"]),
        }
        for row in rows
    )


def attempts_page(
    connection: sqlite3.Connection, *, limit: int, offset: int = 0
) -> tuple[dict[str, Any], ...]:
    """The newest processing attempts, newest first, without the error text."""
    rows = connection.execute(
        f"SELECT {_ATTEMPT_COLUMNS} FROM processing_attempt"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return tuple(
        {
            "id": int(row["id"]),
            "stage": str(row["stage"]),
            "bucket_id": int(row["bucket_id"]),
            "model_id": None if row["model_id"] is None else str(row["model_id"]),
            "model_version": (
                None if row["model_version"] is None else str(row["model_version"])
            ),
            "config_version": (
                None if row["config_version"] is None else str(row["config_version"])
            ),
            "started_at": str(row["started_at"]),
            "finished_at": (
                None if row["finished_at"] is None else str(row["finished_at"])
            ),
            "outcome": str(row["outcome"]),
            "has_error": row["error"] is not None,
        }
        for row in rows
    )
