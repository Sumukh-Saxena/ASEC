"""Bucket processing-state lifecycle for ASEC (Phase 3).

FR-BKT-12: "The system shall record whether each bucket is OPEN, CLOSED,
PROCESSED, FAILED or AWAITING_RECOVERY." The vocabulary is stored verbatim in
migration 0003 (CHECK-enforced). Committed formation code
(:mod:`backend.bucketing`) walks OPEN → CLOSED only — closure of an OPEN
bucket belongs exclusively to accepted per-group event-time progress
(FR-BKT-05, Revision 4) and is never performed here.

This module owns the remaining lifecycle transitions, which the SRS leaves to
service level (0003 header: "transitions belong to later processing slices"):

- CLOSED → PROCESSED / FAILED / AWAITING_RECOVERY (FR-REL-01: the classifier
  runs on each closed bucket; success, permanent failure or an outage pause)
- FAILED → AWAITING_RECOVERY (NFR-REL-03: a permanently failed attempt stays
  recoverable by manual re-queue, which re-enters this queue)
- AWAITING_RECOVERY → PROCESSED / FAILED (processing resumes after recovery —
  NFR-REL-05, AC-13 — and either completes or fails permanently again)

Transitions to the *same* state are idempotent no-ops (NFR-REL-01: a stage is
safe to retry without creating duplicates). No legacy buckets and no unknown
targets are tolerated: an unknown bucket id or an edge outside the table
fails loudly. PROCESSED is terminal apart from its own idempotent no-op, and
no transition ever opens a CLOSED bucket again (FR-BKT-08: closed buckets and
their results do not change). Every change runs inside one
``transaction()`` (single-connection discipline), returns the observed
outcome, and requires the caller to have passed authorization decisions (the
service records state; policy lives with the caller).

The exact edge set is a Phase 3 design decision derived from those SRS
anchors; the SRS text fixes only the state vocabulary (FR-BKT-12). It is
recorded as owner-vetoable in docs/PHASE2_PLAN.md (§7, §12).
"""

from __future__ import annotations

import enum
import sqlite3

from backend.db.connection import transaction


class BucketState(enum.Enum):
    """FR-BKT-12 verbatim tokens; the stored TEXT uses ``.value``."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    AWAITING_RECOVERY = "AWAITING_RECOVERY"


#: Edges this service performs. OPEN↔CLOSED is owned by event-time progress
#: (backend.bucketing) and never by this module.
_SERVICE_EDGES: frozenset[tuple[BucketState, BucketState]] = frozenset(
    {
        (BucketState.CLOSED, BucketState.PROCESSED),
        (BucketState.CLOSED, BucketState.FAILED),
        (BucketState.CLOSED, BucketState.AWAITING_RECOVERY),
        (BucketState.FAILED, BucketState.AWAITING_RECOVERY),
        (BucketState.AWAITING_RECOVERY, BucketState.PROCESSED),
        (BucketState.AWAITING_RECOVERY, BucketState.FAILED),
    }
)


class BucketLifecycleError(Exception):
    """Base class for bucket-lifecycle failures reported to the caller."""


class UnknownBucketError(BucketLifecycleError):
    """No bucket row exists for the given id."""


class InvalidBucketTransitionError(BucketLifecycleError):
    """The requested transition is outside the legal edge set (or the target
    token is not an FR-BKT-12 state). Nothing was written."""


def bucket_processing_state(
    connection: sqlite3.Connection, bucket_id: int
) -> BucketState:
    """Read the current FR-BKT-12 state of ``bucket_id`` (fail loud if absent)."""
    row = connection.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    if row is None:
        raise UnknownBucketError(f"no bucket with id {bucket_id}")
    return BucketState(str(row["processing_state"]))


def transition_bucket_processing_state(
    connection: sqlite3.Connection, bucket_id: int, target: BucketState
) -> bool:
    """Apply one legal lifecycle transition to ``bucket_id`` (FR-BKT-12).

    Returns True when the transition was applied, False when it was an
    idempotent no-op (the bucket was already in ``target`` — retry safety,
    NFR-REL-01). Legal edges per the module docstring; the member state is
    re-read inside the transaction so only the first writer wins and every
    competing writer sees the committed state (FR-BKT-07 discipline, one
    ``BEGIN IMMEDIATE`` writer at a time).
    """
    if not isinstance(target, BucketState):
        raise InvalidBucketTransitionError(
            f"target must be an FR-BKT-12 state, got {target!r}"
        )
    with transaction(connection):
        current = bucket_processing_state(connection, bucket_id)
        if current is target:
            return False
        if (current, target) not in _SERVICE_EDGES:
            raise InvalidBucketTransitionError(
                f"illegal transition {current.name} -> {target.name} for bucket {bucket_id}"
            )
        cursor = connection.execute(
            "UPDATE bucket SET processing_state = ? WHERE id = ? AND processing_state = ?",
            (target.value, bucket_id, current.value),
        )
        # Defense in depth (compare backend.ingest's UNIQUE-conflict
        # fallback): inside one BEGIN IMMEDIATE transaction no other writer
        # can interleave, so a zero rowcount would mean an unprotected
        # caller — fail loud rather than report a success that never wrote.
        if cursor.rowcount != 1:
            raise InvalidBucketTransitionError(
                f"transition lost for bucket {bucket_id} ({current.name} -> {target.name})"
            )
    return True
