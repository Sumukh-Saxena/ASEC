"""W9 H7: import Discord channel -> vertical mappings from a CSV file.

The CSV columns are ``discord_channel_id`` and ``vertical_id`` (blank allowed).
Structural validation and the existing-row comparison complete before any
write; the whole import then runs in one transaction, so a bad row or a conflict
changes nothing. Output is counts only, never a channel id or file content
(FR-ING-09, SRS DR-07).
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.db.connection import transaction

COLUMNS = ("discord_channel_id", "vertical_id")


class GroupImportError(Exception):
    """The file is malformed or a mapping conflicts with stored state."""


@dataclass(frozen=True)
class ImportCounts:
    """Rows added, rows already identical, and conflicting rows."""

    added: int = 0
    unchanged: int = 0
    conflicts: int = 0


@dataclass(frozen=True)
class GroupMapping:
    """One validated CSV row."""

    channel_id: str
    vertical_id: int | None


def read_group_csv(path: Path) -> list[GroupMapping]:
    """Parse and structurally validate the CSV; never returns partial rows."""
    mappings: list[GroupMapping] = []
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as error:
                raise GroupImportError("the file is empty") from error
            if tuple(cell.strip() for cell in header) != COLUMNS:
                raise GroupImportError(
                    "the header must be discord_channel_id,vertical_id"
                )
            for line_number, row in enumerate(reader, start=2):
                if len(row) != len(COLUMNS):
                    raise GroupImportError(
                        f"row {line_number}: expected {len(COLUMNS)} columns"
                    )
                channel_id = row[0].strip()
                if not (
                    channel_id.isascii()
                    and channel_id.isdigit()
                    and 17 <= len(channel_id) <= 20
                ):
                    raise GroupImportError(
                        f"row {line_number}: discord_channel_id must be 17-20 digits"
                    )
                if channel_id in seen:
                    raise GroupImportError(
                        f"row {line_number}: duplicate discord_channel_id"
                    )
                seen.add(channel_id)
                raw_vertical = row[1].strip()
                vertical_id: int | None = None
                if raw_vertical != "":
                    try:
                        vertical_id = int(raw_vertical)
                    except ValueError as error:
                        raise GroupImportError(
                            f"row {line_number}: vertical_id must be an integer"
                        ) from error
                mappings.append(GroupMapping(channel_id, vertical_id))
    except OSError as error:
        raise GroupImportError("the file could not be read") from error
    return mappings


def import_groups(
    connection: sqlite3.Connection, mappings: list[GroupMapping]
) -> ImportCounts:
    """Apply ``mappings`` all-or-nothing; a conflict writes nothing.

    A channel that already has the same vertical (including both blank) is
    unchanged. A channel mapped to a different vertical is a conflict. Every
    vertical id present in the file must already exist in the vertical table.
    """
    existing = {
        str(row["discord_channel_id"]): row["vertical_id"]
        for row in connection.execute(
            "SELECT discord_channel_id, vertical_id FROM source_group"
        ).fetchall()
    }
    verticals = {
        int(row["id"]) for row in connection.execute("SELECT id FROM vertical").fetchall()
    }
    for mapping in mappings:
        if mapping.vertical_id is not None and mapping.vertical_id not in verticals:
            raise GroupImportError("a vertical_id in the file does not exist")

    added: list[GroupMapping] = []
    unchanged = 0
    conflicts = 0
    for mapping in mappings:
        if mapping.channel_id not in existing:
            added.append(mapping)
        elif existing[mapping.channel_id] == mapping.vertical_id:
            unchanged += 1
        else:
            conflicts += 1
    if conflicts:
        # Nothing changes, including the valid additions in the same file.
        return ImportCounts(added=0, unchanged=unchanged, conflicts=conflicts)

    with transaction(connection):
        for mapping in added:
            connection.execute(
                "INSERT INTO source_group (discord_channel_id, vertical_id)"
                " VALUES (?, ?)",
                (mapping.channel_id, mapping.vertical_id),
            )
    return ImportCounts(added=len(added), unchanged=unchanged, conflicts=0)


def import_groups_from_file(
    connection: sqlite3.Connection, path: Path
) -> ImportCounts:
    """Read, validate and apply one CSV file inside a single transaction."""
    return import_groups(connection, read_group_csv(path))
