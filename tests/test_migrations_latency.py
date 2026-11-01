"""Schema-level tests for the 0011_latency_measurements migration (Slice 2.11).

Verifies: migration application/idempotency/failure isolation; the
NFR-PERF-02 recording shape (SRS line 615: "The system shall record ingestion
delay, bucket delay, classifier latency, proposal latency and review waiting
time, as P50, P95 and P99") — one raw measurement row per observation, with
the metric label as non-blank free text (the SRS names the kinds in prose; no
invented token vocabulary per the D1 rule), a non-negative value in seconds
(unit named by the column), a verbatim recorded_at, and exactly one subject
FK — source_message, bucket, processing_attempt or proposal — keeping every
measurement traceable to its authoritative record chain (DR-01, 0008
exactly-one-subject pattern).

Deliberately not enforced here: the metric↔subject-type pairing, how each
value is derived from source timestamps, and percentile computation — all
service-level (Phase 3). The blank §6.4 target cells (SRS lines 608–611) are
owner decisions and are not recorded or invented anywhere. The raw rows
support later computation of P50/P95/P99 (DR-03: derived data never replaces
source data); no percentile rows exist and this slice implements no
computation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_LATENCY_TABLES = {
    "latency_measurement",
}

_RECORDED_AT = "2026-05-01T12:00:00Z"
_GENERATED_AT = "2026-05-01T11:05:00Z"
_DECISION_RUN_AT = "2026-05-01T11:00:00Z"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row["version"] for row in rows]


def _applied_at(connection: sqlite3.Connection, version: int) -> str:
    row = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    assert row is not None
    return str(row["applied_at"])


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


def _add_person(connection: sqlite3.Connection, name: str, role: str) -> int:
    # PB-5: mandatory unique synthetic phone/roll_number for every roster row.
    cursor = connection.execute(
        "INSERT INTO person (name, phone, role, roll_number) VALUES (?, ?, ?, ?)",
        (name, f"ph-{name}", role, f"roll-{name}"),
    )
    return int(cursor.lastrowid or 0)


def _add_vertical(connection: sqlite3.Connection, vertical_id: int, name: str) -> int:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name)
    )
    return vertical_id


def _add_group(connection: sqlite3.Connection, group_id: int = 1, vertical_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (?, ?, ?)",
        (group_id, f"ch-{group_id}", vertical_id),
    )
    return group_id


def _add_message(
    connection: sqlite3.Connection,
    message_id: int = 1,
    group_id: int = 1,
) -> int:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id, received_at)"
        " VALUES (?, 'discord', ?, ?, '2026-05-01T10:00:00Z')",
        (message_id, str(message_id), group_id),
    )
    return message_id


def _add_bucket(connection: sqlite3.Connection, bucket_id: int = 1, group_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO bucket (id, group_id) VALUES (?, ?)", (bucket_id, group_id)
    )
    return bucket_id


def _join_message(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    message_id: int = 1,
    group_id: int = 1,
) -> int:
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id) VALUES (?, ?, ?)",
        (bucket_id, message_id, group_id),
    )
    return message_id


def _attempt(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    *,
    stage: str = "proposal_generation",
    outcome: str = "SUCCEEDED",
    started_at: str = _DECISION_RUN_AT,
    **fields: Any,
) -> int:
    columns = ["stage", "bucket_id", "started_at", "outcome"]
    values: list[Any] = [stage, bucket_id, started_at, outcome]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO processing_attempt ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _proposal(
    connection: sqlite3.Connection,
    attempt_id: int = 1,
    bucket_id: int = 1,
    primary_message_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "attempt_id": attempt_id,
        "bucket_id": bucket_id,
        "primary_message_id": primary_message_id,
        "source_vertical_id": 1,
        "ptype": "TASK",
        "assignment_mode": "EXPLICIT",
        "task_type": "INTERNAL",
        "structurally_valid": 1,
        "generated_at": _GENERATED_AT,
        "model_id": "model-p",
        "model_version": "1.0.0",
        "prompt_version": "prompt-1",
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO proposal ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _seed_message_chain(connection: sqlite3.Connection) -> None:
    _add_vertical(connection, 1, "logistics")
    _add_person(connection, "creator", "AH")
    _add_group(connection)
    _add_message(connection)
    _add_bucket(connection)
    _join_message(connection)


def _measurement(connection: sqlite3.Connection, **fields: Any) -> int:
    record: dict[str, Any] = {
        "metric": "classifier latency",
        "value_seconds": 1.5,
        "recorded_at": _RECORDED_AT,
    }
    if not any(
        name in fields
        for name in (
            "source_message_id",
            "bucket_id",
            "processing_attempt_id",
            "proposal_id",
        )
    ):
        record["source_message_id"] = 1
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO latency_measurement ({', '.join(record)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


# --- migration application ---------------------------------------------------


def test_latency_table_created_after_earlier_migrations(
    conn: sqlite3.Connection,
) -> None:
    assert {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11} <= set(_versions(conn))
    assert _LATENCY_TABLES <= _table_names(conn)


def test_latency_migration_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        at_11 = _applied_at(connection, 11)
        assert apply_migrations(connection) == []
        assert _applied_at(connection, 11) == at_11
    finally:
        connection.close()


def test_failed_later_migration_preserves_earlier_migrations(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        (directory / path.name).write_bytes(path.read_bytes())
    (directory / "0900_seed.sql").write_text(
        "INSERT INTO vertical (id, name) VALUES (900, 'seed-vertical');\n", encoding="utf-8"
    )
    (directory / "0901_broken.sql").write_text(
        "CREATE TABLE ROLLBACK_CHECK (should_never_exist INTEGER PRIMARY KEY);\n"
        "INSERT INTO nonexistent_table (id) VALUES (1);\n",
        encoding="utf-8",
    )
    connection = connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, directory=directory)
        versions = _versions(connection)
        assert versions == sorted(set(versions))
        assert {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11} <= set(versions)
        assert 900 in versions and 901 not in versions
        assert _LATENCY_TABLES <= _table_names(connection)
        assert "ROLLBACK_CHECK" not in _table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM latency_measurement").fetchone()[0] == 0
    finally:
        connection.close()


# --- measurement rows: NFR-PERF-02 recording shape, exactly one subject ------


def test_minimum_measurement_with_message_subject(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    measurement_id = _measurement(
        conn, metric="ingestion delay", value_seconds=0.25, source_message_id=1
    )
    row = _row(conn, "latency_measurement", measurement_id)
    assert row["metric"] == "ingestion delay"
    assert row["value_seconds"] == 0.25
    assert row["recorded_at"] == _RECORDED_AT
    assert row["source_message_id"] == 1
    assert row["bucket_id"] is None
    assert row["processing_attempt_id"] is None
    assert row["proposal_id"] is None


def test_each_subject_type_records_exactly_one_fk(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    attempt_id = _attempt(conn)
    proposal_id = _proposal(conn, attempt_id=attempt_id)

    bucket_measurement = _measurement(
        conn, metric="bucket delay", value_seconds=2.0, bucket_id=1
    )
    assert _row(conn, "latency_measurement", bucket_measurement)["bucket_id"] == 1

    attempt_measurement = _measurement(
        conn, metric="classifier latency", value_seconds=3.25, processing_attempt_id=attempt_id
    )
    assert _row(conn, "latency_measurement", attempt_measurement)[
        "processing_attempt_id"
    ] == attempt_id

    proposal_measurement = _measurement(
        conn, metric="review waiting time", value_seconds=40.0, proposal_id=proposal_id
    )
    assert _row(conn, "latency_measurement", proposal_measurement)["proposal_id"] == proposal_id


def test_zero_or_two_subjects_rejected(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, source_message_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, source_message_id=1, bucket_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, source_message_id=1, processing_attempt_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, source_message_id=1, proposal_id=1)


def test_unknown_subject_rejected(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, source_message_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, bucket_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, processing_attempt_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, proposal_id=999)


def test_metric_label_domain(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, metric="")
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, metric="   ")
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, metric=None)


def test_value_domain(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, value_seconds=None)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, value_seconds=-0.001)
    zero_id = _measurement(conn, value_seconds=0)
    assert _row(conn, "latency_measurement", zero_id)["value_seconds"] == 0


def test_recorded_at_required_non_blank(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, recorded_at="")
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, recorded_at="  ")
    with pytest.raises(sqlite3.IntegrityError):
        _measurement(conn, recorded_at=None)


def test_many_measurements_per_subject_coexist(conn: sqlite3.Connection) -> None:
    """Percentile grain: many raw rows are what later P50/P95/P99 computation
    would aggregate (not implemented by this slice); nothing enforces one
    measurement per subject or per metric."""
    _seed_message_chain(conn)
    _measurement(conn, metric="ingestion delay", value_seconds=0.10)
    _measurement(conn, metric="ingestion delay", value_seconds=0.40)
    _measurement(conn, metric="bucket delay", value_seconds=1.0)
    count = conn.execute(
        "SELECT COUNT(*) FROM latency_measurement WHERE source_message_id = 1"
    ).fetchone()[0]
    assert count == 3


def test_same_metric_for_two_subjects_coexists(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    _attempt(conn)
    _measurement(conn, metric="classifier latency", value_seconds=1.0)
    _measurement(
        conn, metric="classifier latency", value_seconds=2.0, processing_attempt_id=1
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM latency_measurement WHERE metric = 'classifier latency'"
    ).fetchone()[0]
    assert count == 2


def test_deletion_restrict(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    attempt_id = _attempt(conn)
    proposal_id = _proposal(conn, attempt_id=attempt_id)
    _measurement(conn, source_message_id=1)
    _measurement(conn, bucket_id=1)
    _measurement(conn, processing_attempt_id=attempt_id)
    _measurement(conn, proposal_id=proposal_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_message WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bucket WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM processing_attempt WHERE id = ?", (attempt_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM proposal WHERE id = ?", (proposal_id,))
    conn.execute("DELETE FROM latency_measurement")
    count = conn.execute("SELECT COUNT(*) FROM latency_measurement").fetchone()[0]
    assert count == 0


def test_fk_check_clean_on_mixed_workload(conn: sqlite3.Connection) -> None:
    _seed_message_chain(conn)
    attempt_id = _attempt(conn)
    proposal_id = _proposal(conn, attempt_id=attempt_id)
    _measurement(conn, metric="ingestion delay", value_seconds=0.2, source_message_id=1)
    _measurement(conn, metric="bucket delay", value_seconds=1.5, bucket_id=1)
    _measurement(
        conn, metric="proposal latency", value_seconds=9.0, processing_attempt_id=attempt_id
    )
    _measurement(
        conn, metric="review waiting time", value_seconds=22.0, proposal_id=proposal_id
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_second_connection_measurement_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    first = connect(db_path)
    apply_migrations(first)
    _seed_message_chain(first)
    _measurement(first, value_seconds=1.0)
    first.commit()
    second = connect(db_path)
    try:
        _measurement(second, value_seconds=2.0)
        second.commit()
        count = first.execute("SELECT COUNT(*) FROM latency_measurement").fetchone()[0]
        assert count == 2
    finally:
        second.close()
        first.close()
