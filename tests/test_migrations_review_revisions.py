"""Schema-level tests for migration 0015_review_revisions (Phase 6, WS-1).

Verifies the `revision_assignee` table per owner decision OD-REV-3
(`docs/DECISIONS.md` §A) and amendments A2/A6/A14: per-revision assignee
resolution history (FR-REV-03: the original generated version stays
unchanged; DR-03: derived rows never replace source rows), with the
`MANUAL` resolution route for direct roster selection (OD-REV-2) next to
Phase 5's `MENTION` and `NAME_MATCH` routes. A14's partial unique index
keeps one resolved person per revision.

Also verifies the `authorization_denial` table (owner amendment A17,
FR-AUT-06): the review service records a rejected unauthorized attempt
before raising, with no free-text secrets.

The 0012 CHECK shape is mirrored exactly: RESOLVED rows carry a person and
a route; UNRESOLVED/FLAGGED rows carry neither — an implicit guess is
unrepresentable (never a guess). Storage-only: authorization, eligibility
and effective-draft selection are service tests (tests/test_review_service.py).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


_OMITTED: object = object()


def _add_vertical(connection: sqlite3.Connection, vertical_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)",
        (vertical_id, f"vertical-{vertical_id}"),
    )


def _add_group(
    connection: sqlite3.Connection, group_id: int = 1, vertical_id: int = 1
) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (?, ?, ?)",
        (group_id, f"ch-{group_id}", vertical_id),
    )


def _add_message(connection: sqlite3.Connection, message_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " received_at, content) VALUES (?, 'discord', ?, 1, 't', 'kuch karo yaar')",
        (message_id, str(message_id)),
    )


def _add_person(
    connection: sqlite3.Connection,
    name: str = "assignee",
    role: str = "Volunteer",
    discord_user_id: str | None = None,
    person_id: int | None = None,
) -> int:
    # PB-5: mandatory unique synthetic phone/roll_number for every roster row.
    next_id = (
        person_id
        if person_id is not None
        else int(
            connection.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM person"
            ).fetchone()[0]
        )
    )
    if person_id is None:
        cursor = connection.execute(
            "INSERT INTO person (id, name, phone, role, roll_number,"
            " discord_user_id, active)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                next_id,
                name,
                f"+919800008{next_id:03d}",
                role,
                f"REV-{next_id:03d}",
                discord_user_id,
            ),
        )
    else:
        cursor = connection.execute(
            "INSERT INTO person (id, name, phone, role, roll_number,"
            " discord_user_id, active)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                person_id,
                name,
                f"+919800008{next_id:03d}",
                role,
                f"REV-{next_id:03d}",
                discord_user_id,
            ),
        )
    return int(cursor.lastrowid or 0)


def _add_bucket(connection: sqlite3.Connection, bucket_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO bucket (id, group_id) VALUES (?, 1)", (bucket_id,)
    )


def _join_message(
    connection: sqlite3.Connection, bucket_id: int = 1, message_id: int = 1
) -> None:
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (?, ?, 1)",
        (bucket_id, message_id),
    )


def _add_attempt(
    connection: sqlite3.Connection, bucket_id: int = 1
) -> int:
    cursor = connection.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at, outcome)"
        " VALUES ('proposal_generation', ?, 't', 'SUCCEEDED')",
        (bucket_id,),
    )
    return int(cursor.lastrowid or 0)


def _add_proposal(
    connection: sqlite3.Connection,
    attempt_id: int = 1,
    primary_message_id: int = 1,
) -> int:
    cursor = connection.execute(
        "INSERT INTO proposal (attempt_id, bucket_id, primary_message_id,"
        " ptype, description, assignment_mode, task_type, source_vertical_id,"
        " structurally_valid, generated_at, model_id, model_version,"
        " prompt_version)"
        " VALUES (?, 1, ?, 'TASK', 'plant the flags', 'EXPLICIT', 'INTERNAL', 1,"
        " 1, '2026-05-01T11:05:00Z', 'model-p', '1.0.0', 'prompt-1')",
        (attempt_id, primary_message_id),
    )
    return int(cursor.lastrowid or 0)


def _add_revision(
    connection: sqlite3.Connection,
    proposal_id: int,
    revision_number: int,
    *,
    person_id: int = 1,
) -> int:
    cursor = connection.execute(
        "INSERT INTO proposal_revision (proposal_id, revision_number,"
        " person_id, changed_fields, changed_at, description,"
        " assignment_mode, task_type) VALUES (?, ?, ?, '[\"description\"]',"
        " '2026-05-01T12:00:00Z', 'plot the flagpole', 'EXPLICIT', 'INTERNAL')",
        (proposal_id, revision_number, person_id),
    )
    return int(cursor.lastrowid or 0)


def _add_revision_assignee(
    connection: sqlite3.Connection,
    revision_id: int,
    assignee_text: str = "Neha",
    resolution: str = "RESOLVED",
    person_id: int | None | object = _OMITTED,
    resolved_via: str | None | object = _OMITTED,
) -> int:
    record: dict[str, object] = {
        "revision_id": revision_id,
        "assignee_text": assignee_text,
        "resolution": resolution,
    }
    if person_id is not _OMITTED:
        record["person_id"] = person_id
    if resolved_via is not _OMITTED:
        record["resolved_via"] = resolved_via
    columns = ", ".join(str(column) for column in record)
    placeholders = ", ".join("?" for _ in record)
    cursor = connection.execute(
        f"INSERT INTO revision_assignee ({columns}) VALUES ({placeholders})",
        tuple(record.values()),
    )
    return int(cursor.lastrowid or 0)


def _add_denial(
    connection: sqlite3.Connection,
    actor_person_id: int | None = 1,
    operation: str = "PROPOSAL_REVIEW",
    subject_type: str = "PROPOSAL",
    subject_id: int | None = 1,
    reason: str = "NOT_REVIEWER",
    attempted_at: str = "2026-05-01T12:00:00Z",
) -> int:
    cursor = connection.execute(
        "INSERT INTO authorization_denial (actor_person_id, operation,"
        " subject_type, subject_id, reason, attempted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            actor_person_id,
            operation,
            subject_type,
            subject_id,
            reason,
            attempted_at,
        ),
    )
    return int(cursor.lastrowid or 0)


def _seed_with_revision(conn: sqlite3.Connection) -> tuple[int, int]:
    """Vertical + group + persons + proposal + revision.

    Returns (proposal_id, revision_id).
    """
    _add_vertical(conn)
    _add_group(conn)
    _add_person(conn, name="reviewer", role="AH", person_id=1)
    _add_person(conn, name="Neha", role="Volunteer", discord_user_id="2002")
    _add_message(conn)
    _add_bucket(conn)
    _join_message(conn)
    attempt_id = _add_attempt(conn)
    proposal_id = _add_proposal(conn, attempt_id)
    revision_id = _add_revision(conn, proposal_id, 1, person_id=1)
    return proposal_id, revision_id


# --- table existence and idempotency -----------------------------------------


def test_revision_assignee_table_created(conn: sqlite3.Connection) -> None:
    names = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND"
        " name = 'revision_assignee'"
    ).fetchall()
    assert len(names) == 1


def test_authorization_denial_table_created(conn: sqlite3.Connection) -> None:
    names = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND"
        " name = 'authorization_denial'"
    ).fetchall()
    assert len(names) == 1


def test_migration_0015_recorded_once(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT version, name FROM schema_migrations WHERE version = 15"
    ).fetchall()
    assert [(int(row["version"]), str(row["name"])) for row in rows] == [
        (15, "0015_review_revisions")
    ]


# --- CHECK shape: RESOLVED requires person + route ---------------------------


def test_manual_route_resolved_row_storable(conn: sqlite3.Connection) -> None:
    """OD-REV-2/OD-REV-3: direct roster selection is RESOLVED with the
    MANUAL route and an explicit person."""
    _seed_with_revision(conn)
    row_id = _add_revision_assignee(
        conn, revision_id=1, person_id=2, resolved_via="MANUAL"
    )
    assert row_id is not None


def test_mention_and_name_match_routes_still_valid(
    conn: sqlite3.Connection,
) -> None:
    """Phase 5's two routes remain storable per revision; each row carries a
    distinct resolved person (A14: one resolved person per revision)."""
    _seed_with_revision(conn)
    _add_person(conn, name="Rahul", role="Volunteer", discord_user_id="2003")
    mention_id = _add_revision_assignee(
        conn, revision_id=1, assignee_text="Neha", person_id=2,
        resolved_via="MENTION",
    )
    name_match_id = _add_revision_assignee(
        conn, revision_id=1, assignee_text="Rahul", person_id=3,
        resolved_via="NAME_MATCH",
    )
    assert mention_id != name_match_id


def test_resolved_without_person_or_route_rejected(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, resolution="RESOLVED",
                               person_id=None, resolved_via=None)


def test_resolved_without_route_rejected(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, person_id=2, resolved_via=None)


def test_resolved_without_person_rejected(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, person_id=None,
                               resolved_via="MANUAL")


# --- CHECK shape: UNRESOLVED/FLAGGED carry neither person nor route ----------


def test_unresolved_row_storable_without_person_or_route(
    conn: sqlite3.Connection,
) -> None:
    _seed_with_revision(conn)
    row_id = _add_revision_assignee(
        conn, revision_id=1, assignee_text="maybe", resolution="UNRESOLVED",
        person_id=None, resolved_via=None,
    )
    assert row_id is not None


def test_flagged_row_storable_without_person_or_route(
    conn: sqlite3.Connection,
) -> None:
    _seed_with_revision(conn)
    row_id = _add_revision_assignee(
        conn, revision_id=1, assignee_text="nobody", resolution="FLAGGED",
        person_id=None, resolved_via=None,
    )
    assert row_id is not None


def test_unresolved_cannot_carry_person_or_route(
    conn: sqlite3.Connection,
) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, assignee_text="maybe",
                               resolution="UNRESOLVED", person_id=2,
                               resolved_via=None)


def test_flagged_cannot_carry_person_or_route(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, assignee_text="nobody",
                               resolution="FLAGGED", person_id=None,
                               resolved_via="MANUAL")


# --- vocabulary and text shape ------------------------------------------------


def test_invalid_resolution_token_rejected(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, resolution="MAYBE",
                               person_id=2, resolved_via="MANUAL")


def test_invalid_route_token_rejected(conn: sqlite3.Connection) -> None:
    """No invented route: 'GUESS' is not a resolution route (0012/0015)."""
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, resolution="RESOLVED",
                               person_id=2, resolved_via="GUESS")


def test_blank_assignee_text_rejected(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, assignee_text="   ",
                               person_id=2, resolved_via="MANUAL")


# --- A14 partial unique index ------------------------------------------------


def test_duplicate_resolved_person_per_revision_rejected(
    conn: sqlite3.Connection,
) -> None:
    """A14: one resolved person per revision; first occurrence wins before
    storage, and the schema refuses a duplicate."""
    _seed_with_revision(conn)
    _add_revision_assignee(conn, revision_id=1, person_id=2,
                           resolved_via="MANUAL")
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, assignee_text="Neha (again)",
                               person_id=2, resolved_via="MENTION")


def test_same_person_allowed_in_different_revisions(
    conn: sqlite3.Connection,
) -> None:
    """The index is per revision, not global."""
    proposal_id, first_revision = _seed_with_revision(conn)
    second_revision = _add_revision(conn, proposal_id, 2, person_id=1)
    _add_revision_assignee(conn, revision_id=first_revision, person_id=2,
                           resolved_via="MANUAL")
    _add_revision_assignee(conn, revision_id=second_revision, person_id=2,
                           resolved_via="MANUAL")


def test_multiple_null_person_rows_allowed(conn: sqlite3.Connection) -> None:
    """A14's index is partial: UNRESOLVED/FLAGGED rows have no person and
    are not collapsed."""
    _seed_with_revision(conn)
    _add_revision_assignee(conn, revision_id=1, assignee_text="maybe",
                           resolution="UNRESOLVED", person_id=None,
                           resolved_via=None)
    _add_revision_assignee(conn, revision_id=1, assignee_text="nobody else",
                           resolution="FLAGGED", person_id=None,
                           resolved_via=None)


# --- foreign keys (DR-01/DR-02: RESTRICT everywhere) ---------------------------


def test_assignee_references_revision_and_person(
    conn: sqlite3.Connection,
) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=999, person_id=2,
                               resolved_via="MANUAL")
    with pytest.raises(sqlite3.IntegrityError):
        _add_revision_assignee(conn, revision_id=1, person_id=99999,
                               resolved_via="MANUAL")


def test_revision_deletion_restricted(conn: sqlite3.Connection) -> None:
    """DR-02: deleting a referenced person/revision is blocked, not detached."""
    proposal_id, revision_id = _seed_with_revision(conn)
    _add_revision_assignee(conn, revision_id=revision_id, person_id=2,
                           resolved_via="MANUAL")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM proposal_revision WHERE id = ?",
                     (revision_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM proposal WHERE id = ?", (proposal_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = 2")


def test_original_proposal_assignee_rows_survive_revisions(
    conn: sqlite3.Connection,
) -> None:
    """OD-REV-3: revision-keyed assignees are an ADDITIVE table; the 0012
    generated rows are untouched by revision storage (append-only)."""
    _seed_with_revision(conn)
    conn.execute(
        "INSERT INTO proposal_assignee (proposal_id, assignee_text,"
        " resolution, person_id, resolved_via) VALUES"
        " (1, 'Neha', 'RESOLVED', 2, 'NAME_MATCH')"
    )
    _add_revision_assignee(conn, revision_id=1, person_id=2,
                           resolved_via="MANUAL")
    originals = conn.execute(
        "SELECT COUNT(*) AS n FROM proposal_assignee"
    ).fetchone()
    revision_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM revision_assignee"
    ).fetchone()
    assert int(originals["n"]) == 1  # unchanged
    assert int(revision_rows["n"]) == 1


# --- A17 authorization_denial -------------------------------------------------


def test_denial_with_known_actor_storable(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    row_id = _add_denial(conn, actor_person_id=2, reason="NOT_REVIEWER")
    assert row_id is not None


def test_denial_without_known_actor_storable(conn: sqlite3.Connection) -> None:
    """An unknown caller id has no roster FK target; missing is stored as
    missing (DR-04), never guessed."""
    _seed_with_revision(conn)
    row_id = _add_denial(conn, actor_person_id=None, reason="ACTOR_NOT_FOUND")
    assert row_id is not None


def test_denial_unknown_actor_person_rejected(conn: sqlite3.Connection) -> None:
    _seed_with_revision(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_denial(conn, actor_person_id=99999)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation", "   "),
        ("subject_type", ""),
        ("reason", "   "),
        ("attempted_at", ""),
    ],
)
def test_denial_blank_required_text_rejected(
    conn: sqlite3.Connection, field: str, value: str
) -> None:
    _seed_with_revision(conn)
    record: dict[str, object] = {
        "actor_person_id": 1,
        "operation": "PROPOSAL_REVIEW",
        "subject_type": "PROPOSAL",
        "subject_id": 1,
        "reason": "NOT_REVIEWER",
        "attempted_at": "2026-05-01T12:00:00Z",
    }
    record[field] = value
    columns = ", ".join(str(column) for column in record)
    placeholders = ", ".join("?" for _ in record)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO authorization_denial ({columns})"
            f" VALUES ({placeholders})",
            tuple(record.values()),
        )


def test_denial_survives_and_is_not_detached(conn: sqlite3.Connection) -> None:
    """DR-02: a referenced person is not hard-deletable; the denial stays."""
    _seed_with_revision(conn)
    _add_denial(conn, actor_person_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = 2")
    rows = conn.execute("SELECT COUNT(*) AS n FROM authorization_denial").fetchall()
    assert int(rows[0]["n"]) == 1
