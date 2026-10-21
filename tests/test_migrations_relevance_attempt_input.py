"""Schema-level tests for the 0013_relevance_attempt_input migration.

NFR-AUD-02 (§6.2) durable-input guarantee, from the approved F3/F4 batch:
one canonical masked-input artifact per committed classification attempt.
The artifact is the *masked* request (NFR-SEC-04 reach) — unmasked source
content never appears in the stored snapshot; version columns carry the
masking-ruleset and policy identities at capture time.

Storage contract only: schema shape, uniqueness, CHECK and foreign-key
RESTRICT behavior. Serialization rules, capture timing and pass semantics
belong to backend.relevance (tests/test_relevance_service.py, F3 tests).
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


def _group_and_bucket(conn: sqlite3.Connection, state: str = "CLOSED") -> int:
    group = conn.execute(
        "INSERT INTO source_group (discord_channel_id) VALUES ('chan-1')"
    )
    bucket = conn.execute(
        "INSERT INTO bucket (group_id, processing_state) VALUES (?, ?)",
        (group.lastrowid, state),
    )
    return int(bucket.lastrowid or 0)


def _attempt(conn: sqlite3.Connection, bucket_id: int, outcome: str = "SUCCEEDED") -> int:
    cursor = conn.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
        " finished_at, outcome) VALUES ('RELEVANCE_CLASSIFICATION', ?,"
        " '2026-01-01T10:01:00+00:00', '2026-01-01T10:01:01+00:00', ?)",
        (bucket_id, outcome),
    )
    return int(cursor.lastrowid or 0)


def _snapshot(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    ruleset_version: str = "masking-rules-v1",
    policy_version: str = "classification-policy-v1",
    masked_request: str = '{"bucket_id":1}',
    request_sha256: str = "0" * 64,
) -> int:
    cursor = conn.execute(
        "INSERT INTO relevance_attempt_input"
        " (attempt_id, masking_ruleset_version, policy_version, masked_request,"
        "  request_sha256, captured_at) VALUES (?, ?, ?, ?, ?,"
        "  '2026-01-01T10:01:01+00:00')",
        (attempt_id, ruleset_version, policy_version, masked_request, request_sha256),
    )
    return int(cursor.lastrowid or 0)


def test_table_is_created_by_the_migration(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1 FROM relevance_attempt_input")
    # Re-application stays idempotent (migration runner contract).
    apply_migrations(conn)
    conn.execute("SELECT 1 FROM relevance_attempt_input")


def test_snapshot_row_persists_against_a_real_attempt(
    conn: sqlite3.Connection,
) -> None:
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    snapshot_id = _snapshot(conn, attempt_id)
    row = conn.execute(
        "SELECT attempt_id, masking_ruleset_version, policy_version, masked_request,"
        " request_sha256, captured_at FROM relevance_attempt_input WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    assert int(row["attempt_id"]) == attempt_id
    assert str(row["masking_ruleset_version"]) == "masking-rules-v1"
    assert str(row["policy_version"]) == "classification-policy-v1"
    assert str(row["masked_request"]) == '{"bucket_id":1}'
    assert str(row["request_sha256"]) == "0" * 64
    assert str(row["captured_at"]).strip() != ""


def test_attempt_identity_is_unique(conn: sqlite3.Connection) -> None:
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    _snapshot(conn, attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        _snapshot(conn, attempt_id)


def test_attempt_must_exist_restRICT(conn: sqlite3.Connection) -> None:
    _group_and_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _snapshot(conn, 999)


def test_attempt_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    """DR-01/DR-02: the artifact chain never breaks by silent deletion."""
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    _snapshot(conn, attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM processing_attempt WHERE id = ?", (attempt_id,))


@pytest.mark.parametrize(
    "field", ["masking_ruleset_version", "policy_version", "masked_request"]
)
def test_non_blank_fields_enforced(
    conn: sqlite3.Connection, field: str
) -> None:
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    values = {
        "masking_ruleset_version": "masking-rules-v1",
        "policy_version": "classification-policy-v1",
        "masked_request": '{"bucket_id":1}',
        "request_sha256": "0" * 64,
    }
    values[field] = "   "  # blank violates the CHECK
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO relevance_attempt_input"
            " (attempt_id, masking_ruleset_version, policy_version, masked_request,"
            "  request_sha256, captured_at)"
            " VALUES (?, ?, ?, ?, ?, '2026-01-01T10:01:01+00:00')",
            (
                attempt_id,
                values["masking_ruleset_version"],
                values["policy_version"],
                values["masked_request"],
                values["request_sha256"],
            ),
        )
