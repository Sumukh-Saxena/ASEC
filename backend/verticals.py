"""Owner-run vertical seeding from a CSV file.

``roster-load`` refuses a roster row whose vertical id does not exist, and no
migration seeds the real vertical list (``seed-fake`` creates fake ones only for
development). This module gives the owner one small command to create the real
verticals before a roster load.

Three boundaries are deliberate, matching ``roster-check``/``roster-load``:

* The CSV and the target database live outside the repository; the command
  refuses an in-repository path before reading or writing.
* Validation messages carry a CSV row number and a stable issue code only, never
  an id or a name, so a failed run is safe to paste into an issue.
* A dry-run writes nothing. ``--commit`` writes every row in one transaction, so
  a failure mid-load rolls everything back.

This module never edits or deletes a vertical. It is a pre-festival seeding
step, not a vertical management flow (FR-ROS-02 keeps roster data Admin-only and
fixed during the festival).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from backend.db.connection import transaction

#: The exact header, in order. A missing or extra column is ``BAD_HEADER``.
VERTICAL_COLUMNS = ("id", "name")

#: SQLite INTEGER is signed 64-bit; an id outside this range can never be stored.
MAX_VERTICAL_ID = 2**63 - 1


class VerticalError(Exception):
    """The file cannot be read, validated or applied.

    Messages name a file, a database or a rule, never an id or a name.
    """


@dataclass(frozen=True)
class VerticalIssue:
    """One validation problem: a CSV line number and a stable code."""

    row: int
    code: str


@dataclass(frozen=True)
class VerticalRow:
    """One structurally parsed CSV row (may still carry validation issues)."""

    line: int
    id: int
    name: str


@dataclass(frozen=True)
class VerticalReport:
    """The parsed rows and every issue found in one pass."""

    rows: tuple[VerticalRow, ...]
    issues: tuple[VerticalIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_verticals(
    path: str | Path,
    *,
    existing_ids: frozenset[int] | None = None,
    existing_names: frozenset[str] | None = None,
) -> VerticalReport:
    """Read and validate one CSV; never writes and never returns partial rows.

    ``existing_ids``/``existing_names`` are the values already stored in the
    database, so a dry-run predicts the same conflicts a commit would hit.
    """
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError as error:
        raise VerticalError("the file could not be read") from error
    return _validate_rows(
        rows, existing_ids=existing_ids, existing_names=existing_names
    )


def validate_verticals_text(
    text: str,
    *,
    existing_ids: frozenset[int] | None = None,
    existing_names: frozenset[str] | None = None,
) -> VerticalReport:
    """Validate CSV text through the same path as :func:`validate_verticals`."""
    return _validate_rows(
        list(csv.reader(io.StringIO(text))),
        existing_ids=existing_ids,
        existing_names=existing_names,
    )


def _validate_rows(
    rows: list[list[str]],
    *,
    existing_ids: frozenset[int] | None,
    existing_names: frozenset[str] | None,
) -> VerticalReport:
    if not rows:
        return VerticalReport(rows=(), issues=(VerticalIssue(1, "BAD_HEADER"),))
    header = tuple(cell.strip() for cell in rows[0])
    if header != VERTICAL_COLUMNS:
        return VerticalReport(rows=(), issues=(VerticalIssue(1, "BAD_HEADER"),))

    issues: list[VerticalIssue] = []
    parsed: list[VerticalRow] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    known_ids = existing_ids or frozenset()
    known_names = existing_names or frozenset()

    for offset, cells in enumerate(rows[1:], start=2):
        if len(cells) != len(VERTICAL_COLUMNS):
            issues.append(VerticalIssue(offset, "BAD_COLUMNS"))
            continue
        raw_id = cells[0].strip()
        name = cells[1].strip()
        vertical_id = _parse_id(raw_id)

        if vertical_id is None:
            issues.append(VerticalIssue(offset, "BAD_ID"))
        else:
            if vertical_id in seen_ids:
                issues.append(VerticalIssue(offset, "DUPLICATE_ID"))
            else:
                seen_ids.add(vertical_id)
            if vertical_id in known_ids:
                issues.append(VerticalIssue(offset, "EXISTING_ID"))

        if name == "":
            issues.append(VerticalIssue(offset, "EMPTY_NAME"))
        else:
            if name in seen_names:
                issues.append(VerticalIssue(offset, "DUPLICATE_NAME"))
            else:
                seen_names.add(name)
            if name in known_names:
                issues.append(VerticalIssue(offset, "EXISTING_NAME"))

        if vertical_id is not None and name != "":
            parsed.append(VerticalRow(line=offset, id=vertical_id, name=name))

    return VerticalReport(rows=tuple(parsed), issues=tuple(issues))


def _parse_id(raw: str) -> int | None:
    """A positive ASCII integer that fits a SQLite INTEGER, or ``None``."""
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    if value < 1 or value > MAX_VERTICAL_ID:
        return None
    return value


def format_preview(report: VerticalReport) -> str:
    """Render the counts-only preview; never an id or a name."""
    issue_counts = Counter(issue.code for issue in report.issues)
    counts = " ".join(
        f"{code}={count}" for code, count in sorted(issue_counts.items())
    ) or "none"
    return "\n".join(
        (
            f"rows={len(report.rows)} issues={len(report.issues)}",
            f"issue {counts}",
        )
    )


def read_existing_verticals(
    connection: sqlite3.Connection,
) -> tuple[frozenset[int], frozenset[str]]:
    """The ids and names already stored, so a dry-run can predict conflicts."""
    try:
        rows = connection.execute("SELECT id, name FROM vertical").fetchall()
    except sqlite3.Error as error:
        raise VerticalError("the database could not be read") from error
    return (
        frozenset(int(row["id"]) for row in rows),
        frozenset(str(row["name"]) for row in rows),
    )


def apply_verticals(
    connection: sqlite3.Connection, rows: tuple[VerticalRow, ...]
) -> int:
    """Insert every row in one transaction; a failure rolls everything back."""
    if not rows:
        raise VerticalError("the file has no rows")
    with transaction(connection):
        for row in rows:
            connection.execute(
                "INSERT INTO vertical (id, name) VALUES (?, ?)",
                (row.id, row.name),
            )
    return len(rows)
