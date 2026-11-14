"""0023 push endpoints and in-app read markers (FR-NOT-07, EIR-04, NFR-AUD-03).

Pins the migration constraints (provider, unique token, active index, foreign
keys), the endpoint helpers (registration, rotation, deactivation) and the
secret rule: a push token is never echoed in an exception or a repr.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    PushEndpointConflict,
    deactivate_endpoint,
    register_endpoint,
)
from tests.notification_fixtures import (
    add_notification,
    add_proposal,
    add_task,
    seed_message_chain,
    seed_roster,
)

_TOKEN_A = "fake-token-alpha"
_TOKEN_B = "fake-token-bravo"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row["version"]) for row in rows]


def _endpoint(connection: sqlite3.Connection, endpoint_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM push_endpoint WHERE id = ?", (endpoint_id,)
    ).fetchone()
    assert row is not None
    return row


def _notification_id(connection: sqlite3.Connection) -> int:
    seed_roster(connection)
    seed_message_chain(connection)
    add_proposal(connection, 1)
    add_task(connection, 1, proposal_id=1)
    return add_notification(
        connection,
        event_type="TASK_ASSIGNED",
        recipient_person_id=3,
        dedupe_key="TASK_ASSIGNED:1:3",
        task_id=1,
    )


def test_0023_applies_and_reapplies_without_changes(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == list(range(1, 25))
    names = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"push_endpoint", "notification_read"} <= names
    assert apply_migrations(conn) == []
    assert _versions(conn) == list(range(1, 25))


def test_push_endpoint_provider_is_fcm_only(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    conn.execute(
        "INSERT INTO push_endpoint (person_id, provider, token, created_at)"
        " VALUES (3, 'FCM', 'fake-token-one', '2026-11-01T10:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO push_endpoint (person_id, provider, token, created_at)"
            " VALUES (3, 'APNS', 'fake-token-two', '2026-11-01T10:00:00+00:00')"
        )


def test_push_endpoint_token_is_unique(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    conn.execute(
        "INSERT INTO push_endpoint (person_id, provider, token, created_at)"
        " VALUES (3, 'FCM', 'fake-token-one', '2026-11-01T10:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO push_endpoint (person_id, provider, token, created_at)"
            " VALUES (4, 'FCM', 'fake-token-one', '2026-11-01T10:00:00+00:00')"
        )


@pytest.mark.parametrize(
    ("token", "active"),
    [("   ", 1), ("fake-token", 2)],
)
def test_push_endpoint_token_and_active_domains(
    conn: sqlite3.Connection, token: str, active: int
) -> None:
    seed_roster(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO push_endpoint (person_id, provider, token, active, created_at)"
            " VALUES (3, 'FCM', ?, ?, '2026-11-01T10:00:00+00:00')",
            (token, active),
        )


def test_push_endpoint_person_foreign_key(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO push_endpoint (person_id, provider, token, created_at)"
            " VALUES (999, 'FCM', 'fake-token-one', '2026-11-01T10:00:00+00:00')"
        )


def test_active_endpoint_index_is_partial(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index'"
        " AND name = 'ix_push_endpoint_active'"
    ).fetchone()
    assert row is not None
    sql = str(row["sql"]).lower()
    assert "push_endpoint" in sql
    assert "person_id" in sql
    assert "where active = 1" in sql


def test_notification_read_is_immutable_and_foreign_keyed(
    conn: sqlite3.Connection,
) -> None:
    notification_id = _notification_id(conn)
    conn.execute(
        "INSERT INTO notification_read (notification_id, read_at) VALUES (?, ?)",
        (notification_id, "2026-11-01T10:05:00+00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notification_read (notification_id, read_at) VALUES (?, ?)",
            (notification_id, "2026-11-01T10:06:00+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notification_read (notification_id, read_at)"
            " VALUES (?, ' ')",
            (notification_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notification_read (notification_id, read_at)"
            " VALUES (999, '2026-11-01T10:05:00+00:00')"
        )


def test_register_endpoint_inserts_one_active_row(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    endpoint_id = register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:00:00+00:00"
    )
    row = _endpoint(conn, endpoint_id)
    assert int(row["person_id"]) == 3
    assert str(row["provider"]) == "FCM"
    assert int(row["active"]) == 1
    assert str(row["created_at"]) == "2026-11-01T10:00:00+00:00"
    assert str(row["last_seen_at"]) == "2026-11-01T10:00:00+00:00"


def test_register_same_token_updates_in_place(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    first = register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:00:00+00:00"
    )
    second = register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:10:00+00:00"
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM push_endpoint").fetchone()[0] == 1
    assert str(_endpoint(conn, first)["last_seen_at"]) == "2026-11-01T10:10:00+00:00"


def test_rotated_token_deactivates_the_old_row(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    old_id = register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:00:00+00:00"
    )
    new_id = register_endpoint(
        conn, person_id=3, token=_TOKEN_B, now="2026-11-01T10:05:00+00:00"
    )
    assert old_id != new_id
    assert int(_endpoint(conn, old_id)["active"]) == 0
    assert int(_endpoint(conn, new_id)["active"]) == 1
    active = conn.execute(
        "SELECT id FROM push_endpoint WHERE person_id = 3 AND active = 1"
    ).fetchall()
    assert [int(row["id"]) for row in active] == [new_id]


def test_register_token_for_another_person_conflicts_without_leaking_it(
    conn: sqlite3.Connection,
) -> None:
    seed_roster(conn)
    register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:00:00+00:00"
    )
    with pytest.raises(PushEndpointConflict) as caught:
        register_endpoint(
            conn, person_id=4, token=_TOKEN_A, now="2026-11-01T10:05:00+00:00"
        )
    assert _TOKEN_A not in str(caught.value)
    assert _TOKEN_A not in repr(caught.value)


def test_deactivate_endpoint_requires_ownership(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    endpoint_id = register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:00:00+00:00"
    )
    assert deactivate_endpoint(conn, endpoint_id, person_id=4) is False
    assert int(_endpoint(conn, endpoint_id)["active"]) == 1
    assert deactivate_endpoint(conn, endpoint_id, person_id=3) is True
    assert int(_endpoint(conn, endpoint_id)["active"]) == 0
    assert deactivate_endpoint(conn, endpoint_id, person_id=3) is False


def test_register_endpoint_rejects_blank_token_without_echoing_it(
    conn: sqlite3.Connection,
) -> None:
    seed_roster(conn)
    with pytest.raises(ValueError) as caught:
        register_endpoint(conn, person_id=3, token="   ")
    assert "   " not in str(caught.value)


def test_push_endpoint_integrity_checks_are_clean(conn: sqlite3.Connection) -> None:
    seed_roster(conn)
    register_endpoint(
        conn, person_id=3, token=_TOKEN_A, now="2026-11-01T10:00:00+00:00"
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
