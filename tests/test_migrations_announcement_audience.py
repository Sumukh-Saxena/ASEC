"""Schema-level tests for 0018_announcement_audience (Phase 7, PB-6).

FR-REV-10 says an accepted announcement is shown to its audience: a Head's
vertical, or the sender AH's volunteers. 0018 replaces the free-text
``audience_scope`` with the two-token ``audience_type``, a mandatory
``audience_vertical_id`` and the ``audience_ah_person_id`` snapshot of the
sender whose volunteers the announcement targets.

The tests drive the real migration over pre-window rows, so the copy rules, the
abort guards and the resulting audience snapshot are proven on real data. A row
whose audience cannot be resolved aborts the migration; it never becomes a
silently empty distribution list.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_PRE_WINDOW = 15
_ACCEPTED_AT = "2026-05-01T12:00:00Z"


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


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: row for row in rows}


def _person(connection: sqlite3.Connection, person_id: int, name: str, role: str) -> int:
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, ?, 1, ?)",
        (person_id, name, f"ph-{person_id}", role, f"roll-{person_id}"),
    )
    return person_id


def _chain(
    connection: sqlite3.Connection,
    *,
    message_id: int,
    sender_person_id: int | None,
) -> int:
    """One bucket, one message and one structurally valid ANNOUNCEMENT proposal."""
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, received_at)"
        " VALUES (?, 'discord', ?, 1, ?, '2026-05-01T10:00:00Z')",
        (message_id, f"m-{message_id}", sender_person_id),
    )
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (1, ?, 1)",
        (message_id,),
    )
    cursor = connection.execute(
        "INSERT INTO proposal (attempt_id, bucket_id, primary_message_id,"
        " source_vertical_id, ptype, assignment_mode, task_type,"
        " structurally_valid, generated_at, model_id, model_version,"
        " prompt_version)"
        " VALUES (1, 1, ?, 1, 'ANNOUNCEMENT', 'NONE', 'INTERNAL', 1,"
        " '2026-05-01T11:05:00Z', 'model-p', '1.0.0', 'prompt-1')",
        (message_id,),
    )
    return int(cursor.lastrowid or 0)


def _old_announcement(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    accepted_by: int = 1,
    audience_vertical_id: int | None = 1,
    audience_scope: str = "VERTICAL",
) -> int:
    cursor = connection.execute(
        "INSERT INTO announcement (proposal_id, accepted_by, accepted_at,"
        " audience_vertical_id, audience_scope) VALUES (?, ?, ?, ?, ?)",
        (proposal_id, accepted_by, _ACCEPTED_AT, audience_vertical_id, audience_scope),
    )
    return int(cursor.lastrowid or 0)


def _seed_roster(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'logistics')")
    connection.execute("INSERT INTO vertical (id, name) VALUES (2, 'decor')")
    _person(connection, 1, "Head-Human", "Head")
    _person(connection, 2, "Ah-Sender", "AH")
    _person(connection, 3, "Ah-Other", "AH")
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'ch-1', 1)"
    )
    connection.execute("INSERT INTO bucket (id, group_id) VALUES (1, 1)")
    connection.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at, outcome)"
        " VALUES ('proposal_generation', 1, '2026-05-01T10:30:00Z', 'SUCCEEDED')"
    )


def _new_announcement(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    audience_type: str = "VERTICAL",
    audience_vertical_id: int = 1,
    audience_ah_person_id: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO announcement (proposal_id, accepted_by, accepted_at,"
        " audience_type, audience_vertical_id, audience_ah_person_id)"
        " VALUES (?, 1, ?, ?, ?, ?)",
        (proposal_id, _ACCEPTED_AT, audience_type, audience_vertical_id,
         audience_ah_person_id),
    )


@pytest.fixture()
def pre_window(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "announcement-audience.db")
    apply_migrations(connection, directory=_staged_directory(tmp_path, _PRE_WINDOW))
    _seed_roster(connection)
    yield connection
    connection.close()


# --- window application -----------------------------------------------------


def test_0018_lands_in_the_window_and_reapply_is_a_noop(
    pre_window: sqlite3.Connection,
) -> None:
    """PB-6: 0018 applies with 0016/0017 and re-running changes nothing."""
    assert _versions(pre_window) == list(range(1, _PRE_WINDOW + 1))

    assert apply_migrations(pre_window) == [16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
    assert _versions(pre_window) == list(range(1, 26))
    assert apply_migrations(pre_window) == []


# --- copy rules -------------------------------------------------------------


def test_vertical_announcement_keeps_the_vertical_audience(
    pre_window: sqlite3.Connection,
) -> None:
    """FR-REV-10: a Head's announcement addresses the source vertical."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=1)
    _old_announcement(pre_window, proposal_id, audience_scope="VERTICAL")

    apply_migrations(pre_window)

    row = pre_window.execute("SELECT * FROM announcement").fetchone()
    assert row is not None
    assert str(row["audience_type"]) == "VERTICAL"
    assert row["audience_vertical_id"] == 1
    assert row["audience_ah_person_id"] is None


def test_ah_announcement_snapshots_the_primary_sender(
    pre_window: sqlite3.Connection,
) -> None:
    """FR-REV-10/PB-6: the AH audience is the sender of the primary message."""
    proposal_id = _chain(pre_window, message_id=2, sender_person_id=3)
    _old_announcement(
        pre_window,
        proposal_id,
        accepted_by=1,
        audience_scope="AH_VOLUNTEERS",
        audience_vertical_id=1,
    )

    apply_migrations(pre_window)

    row = pre_window.execute("SELECT * FROM announcement").fetchone()
    assert row is not None
    assert str(row["audience_type"]) == "AH_VOLUNTEERS"
    assert row["audience_vertical_id"] == 1
    assert row["audience_ah_person_id"] == 3


def test_free_text_audience_column_is_replaced(pre_window: sqlite3.Connection) -> None:
    """PB-6: the old free-text column is gone; the new audience columns exist."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=2)
    _old_announcement(pre_window, proposal_id, audience_scope="AH_VOLUNTEERS")

    apply_migrations(pre_window)

    columns = _columns(pre_window, "announcement")
    assert "audience_scope" not in columns
    assert columns["audience_type"]["notnull"] == 1
    assert columns["audience_vertical_id"]["notnull"] == 1
    assert columns["audience_ah_person_id"]["notnull"] == 0


# --- abort guards on pre-window rows ---------------------------------------


def test_aborts_on_an_unknown_audience_scope(pre_window: sqlite3.Connection) -> None:
    """PB-6: a scope outside the two tokens aborts instead of becoming garbage."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=1)
    _old_announcement(
        pre_window, proposal_id, audience_scope="sender AH's volunteers"
    )

    with pytest.raises(sqlite3.IntegrityError, match="announcement_audience_type_known"):
        apply_migrations(pre_window)


def test_aborts_when_the_audience_vertical_is_missing(
    pre_window: sqlite3.Connection,
) -> None:
    """PB-6/FR-REV-10: an announcement with no vertical audience aborts."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=1)
    _old_announcement(
        pre_window, proposal_id, audience_scope="VERTICAL", audience_vertical_id=None
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="announcement_audience_vertical_required"
    ):
        apply_migrations(pre_window)


def test_aborts_when_the_ah_sender_cannot_be_resolved(
    pre_window: sqlite3.Connection,
) -> None:
    """FR-REV-10/PB-6: an AH audience without a sender would reach nobody."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=None)
    _old_announcement(pre_window, proposal_id, audience_scope="AH_VOLUNTEERS")

    with pytest.raises(
        sqlite3.IntegrityError, match="announcement_ah_sender_required"
    ):
        apply_migrations(pre_window)


def test_a_failed_rebuild_keeps_version_15_and_the_old_shape(
    pre_window: sqlite3.Connection,
) -> None:
    """Atomicity: the aborted window leaves version 15 and the old column set."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=1)
    _old_announcement(pre_window, proposal_id, audience_scope="mystery")

    with pytest.raises(sqlite3.IntegrityError):
        apply_migrations(pre_window)

    assert _versions(pre_window) == list(range(1, _PRE_WINDOW + 1))
    columns = _columns(pre_window, "announcement")
    assert "audience_scope" in columns
    assert "audience_type" not in columns
    assert pre_window.execute("SELECT count(*) FROM announcement").fetchone()[0] == 1


# --- forward constraints ----------------------------------------------------


def test_new_rows_require_a_resolvable_audience(pre_window: sqlite3.Connection) -> None:
    """FR-REV-10/PB-6: the rebuilt table only accepts complete audiences."""
    first = _chain(pre_window, message_id=1, sender_person_id=2)
    second = _chain(pre_window, message_id=2, sender_person_id=2)
    third = _chain(pre_window, message_id=3, sender_person_id=2)
    fourth = _chain(pre_window, message_id=4, sender_person_id=2)
    apply_migrations(pre_window)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _new_announcement(pre_window, first, audience_type="EVERYONE")
    with pytest.raises(sqlite3.IntegrityError, match="audience_type"):
        _new_announcement(
            pre_window, second, audience_type="AH_VOLUNTEERS", audience_ah_person_id=None
        )
    with pytest.raises(sqlite3.IntegrityError, match="audience_vertical_id"):
        _new_announcement(pre_window, third, audience_vertical_id=None)
    _new_announcement(pre_window, fourth)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _new_announcement(pre_window, fourth)


def test_rebuild_keeps_the_announcement_links(pre_window: sqlite3.Connection) -> None:
    """DR-01/DR-02: proposal, accepting person and vertical references survive."""
    proposal_id = _chain(pre_window, message_id=1, sender_person_id=3)
    _old_announcement(
        pre_window, proposal_id, accepted_by=1, audience_scope="AH_VOLUNTEERS"
    )

    apply_migrations(pre_window)

    assert pre_window.execute("PRAGMA foreign_key_check").fetchall() == []
    row = pre_window.execute("SELECT * FROM announcement").fetchone()
    assert row is not None
    assert row["proposal_id"] == proposal_id
    assert row["accepted_by"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        pre_window.execute("DELETE FROM person WHERE id = 3")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        pre_window.execute("DELETE FROM vertical WHERE id = 1")
