"""Schema-level tests for 0017_task_schema (Phase 7, PB-3).

0017 rebuilds ``task`` and ``task_assignee`` for FR-TSK-01/02/09 and FR-TSK-03/
05: ``responsible_vertical_id`` becomes mandatory and equal to the source
vertical for ``INTERNAL`` tasks, ``EXPLICIT`` tasks carry ``people_needed NULL``
(the encoding FR-TSK-04 needs after PB-3), and a partial unique index makes an
active ``(task_id, person_id)`` pair unique while a denied or cancelled record
still allows re-adding the same person (FR-TSK-05).

Every non-conforming pre-window state named in the plan is already excluded by
0006's CHECK constraints (``CROSS_VERTICAL`` requires a different non-NULL
responsible vertical, ``EXPLICIT`` requires ``people_needed = 1``, and
``UNIQUE (task_id, person_id)`` forbids duplicate assignee pairs), so the copy
rules cannot silently mangle a legal pre-window row; the rebuilt table's own
constraints and the new index are the forward guard.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_PRE_WINDOW = 15
_CREATED_AT = "2026-05-01T12:00:00Z"
_GENERATED_AT = "2026-05-01T10:00:00Z"
_RUN_AT = "2026-05-01T10:30:00Z"


def _staged_directory(tmp_path: Path, limit: int) -> Path:
    directory = tmp_path / f"migrations-{limit}"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) <= limit:
            (directory / path.name).write_bytes(path.read_bytes())
    return directory


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row["version"] for row in rows]


def _person(connection: sqlite3.Connection, person_id: int, name: str) -> None:
    """A pre-window person row carrying the identity 0016 will require."""
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, 'Volunteer', 1, ?)",
        (person_id, name, f"ph-{person_id}", f"roll-{person_id}"),
    )


def _seed_chain(connection: sqlite3.Connection) -> int:
    """Verticals, people, one group, four messages, one bucket, one attempt."""
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'logistics')")
    connection.execute("INSERT INTO vertical (id, name) VALUES (2, 'decor')")
    for person_id, name in enumerate(("Creator", "Volunteer-A", "Volunteer-B"), start=1):
        _person(connection, person_id, name)
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'ch-1', 1)"
    )
    for message_id in range(1, 5):
        connection.execute(
            "INSERT INTO source_message (id, source, source_message_id, group_id,"
            " received_at) VALUES (?, 'discord', ?, 1, '2026-05-01T10:00:00Z')",
            (message_id, f"m-{message_id}"),
        )
    connection.execute("INSERT INTO bucket (id, group_id) VALUES (1, 1)")
    for message_id in range(1, 5):
        connection.execute(
            "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
            " VALUES (1, ?, 1)",
            (message_id,),
        )
    cursor = connection.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at, outcome)"
        " VALUES ('proposal_generation', 1, ?, 'SUCCEEDED')",
        (_RUN_AT,),
    )
    return int(cursor.lastrowid or 0)


def _proposal(
    connection: sqlite3.Connection,
    attempt_id: int,
    primary_message_id: int,
    *,
    assignment_mode: str = "EXPLICIT",
    task_type: str = "INTERNAL",
) -> int:
    cursor = connection.execute(
        "INSERT INTO proposal (attempt_id, bucket_id, primary_message_id,"
        " source_vertical_id, ptype, assignment_mode, task_type,"
        " structurally_valid, generated_at, model_id, model_version,"
        " prompt_version)"
        " VALUES (?, 1, ?, 1, 'TASK', ?, ?, 1, ?, 'model-p', '1.0.0', 'prompt-1')",
        (attempt_id, primary_message_id, assignment_mode, task_type, _GENERATED_AT),
    )
    return int(cursor.lastrowid or 0)


def _task(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    assignment_mode: str = "EXPLICIT",
    task_type: str = "INTERNAL",
    responsible_vertical_id: int | None = None,
    people_needed: int | None = 1,
    people_needed_unlimited: int = 0,
) -> int:
    cursor = connection.execute(
        "INSERT INTO task (proposal_id, task_type, source_vertical_id,"
        " responsible_vertical_id, creator_id, description, assignment_mode,"
        " people_needed, people_needed_unlimited, created_at)"
        " VALUES (?, ?, 1, ?, 1, 'clip the wires', ?, ?, ?, ?)",
        (
            proposal_id,
            task_type,
            responsible_vertical_id,
            assignment_mode,
            people_needed,
            people_needed_unlimited,
            _CREATED_AT,
        ),
    )
    return int(cursor.lastrowid or 0)


def _assignee(
    connection: sqlite3.Connection,
    task_id: int,
    person_id: int,
    *,
    agreement: str = "AWAITING",
    agreed_at: str | None = None,
    cancelled: int = 0,
) -> None:
    connection.execute(
        "INSERT INTO task_assignee (task_id, person_id, agreement, agreed_at,"
        " work_status, cancelled, added_at)"
        " VALUES (?, ?, ?, ?, 'NOT_STARTED', ?, ?)",
        (task_id, person_id, agreement, agreed_at, cancelled, _CREATED_AT),
    )


@pytest.fixture()
def migrated(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A window-applied database holding one task of every supported shape."""
    connection = connect(tmp_path / "task-schema.db")
    apply_migrations(connection, directory=_staged_directory(tmp_path, _PRE_WINDOW))
    attempt_id = _seed_chain(connection)
    internal_explicit = _task(connection, _proposal(connection, attempt_id, 1))
    cross_explicit = _task(
        connection,
        _proposal(connection, attempt_id, 2, task_type="CROSS_VERTICAL"),
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
    )
    internal_open = _task(
        connection,
        _proposal(connection, attempt_id, 3, assignment_mode="OPEN"),
        assignment_mode="OPEN",
        people_needed=2,
    )
    cross_open = _task(
        connection,
        _proposal(connection, attempt_id, 4, assignment_mode="OPEN", task_type="CROSS_VERTICAL"),
        assignment_mode="OPEN",
        task_type="CROSS_VERTICAL",
        responsible_vertical_id=2,
        people_needed=None,
        people_needed_unlimited=1,
    )
    _assignee(connection, internal_explicit, 2)
    _assignee(connection, cross_explicit, 3, agreement="ACCEPTED", agreed_at=_RUN_AT)
    _assignee(connection, internal_open, 2, agreement="DENIED", agreed_at=_RUN_AT)
    _assignee(connection, cross_open, 3, cancelled=1)
    connection.execute(
        "INSERT INTO task_history (task_id, field_name, changed_at, actor_person_id,"
        " actor_role, reason) VALUES (?, 'TASK_CREATED', ?, 1, 'AH',"
        " 'PROPOSAL_ACCEPTED')",
        (internal_explicit, _CREATED_AT),
    )

    assert apply_migrations(connection) == [16, 17, 18, 19, 20, 21]
    yield connection
    connection.close()


def _task_row(connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    return row


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def test_0017_lands_in_the_window_and_reapply_is_a_noop(
    migrated: sqlite3.Connection,
) -> None:
    """PB-3: 0017 applies with 0016/0018 and re-running changes nothing."""
    assert _versions(migrated) == list(range(1, 22))
    assert apply_migrations(migrated) == []


def test_internal_task_copies_the_source_vertical_as_responsible(
    migrated: sqlite3.Connection,
) -> None:
    """FR-TSK-01/09, PB-3: INTERNAL tasks carry their source vertical, never NULL."""
    columns = migrated.execute("PRAGMA table_info(task)").fetchall()
    responsible = {row["name"]: row for row in columns}["responsible_vertical_id"]
    assert responsible["notnull"] == 1

    for task_id in (1, 3):
        task = _task_row(migrated, task_id)
        assert str(task["task_type"]) == "INTERNAL"
        assert task["responsible_vertical_id"] == task["source_vertical_id"] == 1


def test_cross_vertical_task_keeps_its_responsible_vertical(
    migrated: sqlite3.Connection,
) -> None:
    """FR-TSK-01/FR-XV-01: the copy preserves the approved responsible vertical."""
    for task_id in (2, 4):
        task = _task_row(migrated, task_id)
        assert str(task["task_type"]) == "CROSS_VERTICAL"
        assert task["responsible_vertical_id"] == 2
        assert task["source_vertical_id"] == 1


def test_explicit_task_people_needed_becomes_null(
    migrated: sqlite3.Connection,
) -> None:
    """PB-3/FR-TSK-04: an EXPLICIT task has no numeric people-needed count."""
    for task_id in (1, 2):
        task = _task_row(migrated, task_id)
        assert str(task["assignment_mode"]) == "EXPLICIT"
        assert task["people_needed"] is None
        assert int(task["people_needed_unlimited"]) == 0


def test_open_task_keeps_its_people_needed_encoding(
    migrated: sqlite3.Connection,
) -> None:
    """FR-TSK-01/FR-TSK-04: OPEN tasks keep a count or the unlimited marker."""
    counted = _task_row(migrated, 3)
    assert counted["people_needed"] == 2
    assert int(counted["people_needed_unlimited"]) == 0

    unlimited = _task_row(migrated, 4)
    assert unlimited["people_needed"] is None
    assert int(unlimited["people_needed_unlimited"]) == 1


def test_assignee_rows_survive_the_rebuild(migrated: sqlite3.Connection) -> None:
    """FR-TSK-03/05, DR-02: agreement, work status and cancellation are kept."""
    rows = migrated.execute(
        "SELECT * FROM task_assignee ORDER BY task_id, person_id"
    ).fetchall()
    assert [(row["task_id"], row["person_id"]) for row in rows] == [
        (1, 2),
        (2, 3),
        (3, 2),
        (4, 3),
    ]
    assert [row["agreement"] for row in rows] == [
        "AWAITING",
        "ACCEPTED",
        "DENIED",
        "AWAITING",
    ]
    assert [row["work_status"] for row in rows] == ["NOT_STARTED"] * 4
    assert [int(row["cancelled"]) for row in rows] == [0, 0, 0, 1]
    assert rows[0]["agreed_at"] is None
    assert rows[1]["agreed_at"] == _RUN_AT
    assert rows[2]["agreed_at"] == _RUN_AT


def test_partial_unique_index_blocks_a_duplicate_active_assignee(
    migrated: sqlite3.Connection,
) -> None:
    """FR-TSK-03/05: one active record per person per task, enforced by the index."""
    index_rows = migrated.execute("PRAGMA index_list(task_assignee)").fetchall()
    targeted = [row for row in index_rows if row["name"] == "ux_task_assignee_active"]
    assert targeted, "ux_task_assignee_active missing"
    assert int(targeted[0]["partial"]) == 1
    columns = [
        row["name"]
        for row in migrated.execute(
            "PRAGMA index_info(ux_task_assignee_active)"
        ).fetchall()
    ]
    assert columns == ["task_id", "person_id"]

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _assignee(migrated, 1, 2)


def test_readding_a_denied_or_cancelled_person_is_allowed(
    migrated: sqlite3.Connection,
) -> None:
    """FR-TSK-05: denial frees the place; a cancelled record no longer counts."""
    _assignee(migrated, 3, 2, agreement="AWAITING")
    _assignee(migrated, 4, 3, agreement="AWAITING")
    assert _count(migrated, "task_assignee") == 6

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _assignee(migrated, 3, 2)


def test_responsible_vertical_must_match_the_task_type(
    migrated: sqlite3.Connection,
) -> None:
    """FR-TSK-01/FR-XV-01: the rebuilt table refuses contradictory rows."""
    proposal_id = _proposal(migrated, 1, 1, task_type="CROSS_VERTICAL")
    with pytest.raises(sqlite3.IntegrityError, match="responsible_vertical_id"):
        _task(
            migrated,
            proposal_id,
            task_type="CROSS_VERTICAL",
            responsible_vertical_id=None,
        )

    proposal_id = _proposal(migrated, 1, 1, task_type="CROSS_VERTICAL")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _task(
            migrated,
            proposal_id,
            task_type="CROSS_VERTICAL",
            responsible_vertical_id=1,
        )


def test_explicit_task_cannot_carry_a_people_needed_count(
    migrated: sqlite3.Connection,
) -> None:
    """PB-3/FR-TSK-04: EXPLICIT keeps the NULL encoding for new rows too."""
    proposal_id = _proposal(migrated, 1, 1)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _task(migrated, proposal_id, people_needed=1, responsible_vertical_id=1)


def test_task_children_keep_their_foreign_keys_to_the_rebuilt_table(
    migrated: sqlite3.Connection,
) -> None:
    """DR-01: history and attribute rows still resolve to ``task`` by name."""
    migrated.execute("PRAGMA foreign_keys = ON")
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("task_history", "task_attribute", "task_affected_vertical",
                  "approval_request", "notification"):
        sql = migrated.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
        assert "REFERENCES task" in sql, table

    rows = migrated.execute(
        "SELECT * FROM task_history WHERE task_id = 1"
    ).fetchall()
    assert len(rows) == 1
    migrated.execute(
        "INSERT INTO task_history (task_id, field_name, changed_at,"
        " actor_person_id, actor_role, reason)"
        " VALUES (1, 'work_status', ?, 2, 'Volunteer', 'performer completed')",
        (_RUN_AT,),
    )
    assert _count(migrated, "task_history") == 2
