"""Validated roster loader (C10; FR-ROS-01/02/03, FR-AUT-05, NFR-SEC-03/06).

The roster is loaded by script before the festival (FR-ROS-01, O12-4); there is
no roster API. A wrong role grants wrong authority, a wrong Discord id sends a
sign-in code to the wrong person, and a broken reporting line breaks hierarchy
visibility. This module therefore validates the whole file first, reports every
problem by CSV row number and issue code, and writes nothing unless every row
is valid.

Two boundaries are deliberate:

* Validation messages carry a row number and a code only. They never contain a
  roll number, name, phone, Discord id or any other roster value, so a failed
  load is safe to paste into an issue.
* The load runs in one transaction and refuses a non-empty ``person`` table.
  The MVP loads the roster once into a fresh database; a roster change before
  the festival means loading into a fresh database (assumption recorded in
  ``docs/ROSTER.md``).

The fake-roster builder is here too so ``seed-fake`` uses the same parser,
validator and insert path as a real load. Fake Discord ids carry the
``fake-`` prefix; the console OTP sender refuses anything else.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from backend.db.connection import connect, transaction
from backend.db.migrations import apply_migrations

#: The exact header, in order. A missing or extra column is ``BAD_HEADER``.
ROSTER_COLUMNS = (
    "roll_number",
    "name",
    "discord_user_id",
    "phone",
    "role",
    "reports_to_roll_number",
    "verticals",
    "active",
)

#: The four roster roles (0016's CHECK makes any other value unreachable).
VALID_ROLES = ("Admin", "Head", "AH", "Volunteer")

#: The marker that makes a Discord id obviously fake (seed-fake only).
FAKE_DISCORD_PREFIX = "fake-"

_ROLE_BY_CASEFOLD = {role.casefold(): role for role in VALID_ROLES}

#: The repository root (``backend/roster.py`` -> ``backend`` -> repo root). A
#: real roster and the database that receives it never live in source control.
_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

#: A real Discord snowflake is 17-20 digits today; the seed's ``fake-`` ids are
#: the only exception and only when ``allow_fake_ids`` is set.
MIN_DISCORD_ID_LENGTH = 17
MAX_DISCORD_ID_LENGTH = 20

#: Phone numbers are not tied to one country by the SRS, so only a plausible
#: digit count and punctuation set are checked. E.164 caps a number at 15
#: digits; 7 is the shortest plausible national number.
MIN_PHONE_DIGITS = 7
MAX_PHONE_DIGITS = 15
_PHONE_PUNCTUATION = frozenset("+-() .")

#: The fake verticals ``seed-fake`` creates before loading its roster.
FAKE_VERTICALS: tuple[tuple[int, str], ...] = (
    (1, "Fake Logistics"),
    (2, "Fake Decor"),
    (3, "Fake Food"),
    (4, "Fake Tech"),
)

DEFAULT_FAKE_PEOPLE = 12
MIN_FAKE_PEOPLE = 7


class RosterError(Exception):
    """The roster cannot be read, validated or applied.

    Messages are safe to print: they name a file, a database or a rule, never a
    roster value.
    """


@dataclass(frozen=True)
class RosterIssue:
    """One validation problem: a CSV line number and a stable code."""

    row: int
    code: str


@dataclass(frozen=True)
class RosterRow:
    """One structurally parsed CSV row (may still carry validation issues)."""

    line: int
    roll_number: str
    name: str
    discord_user_id: str
    phone: str
    role: str
    reports_to_roll_number: str | None
    vertical_ids: tuple[int, ...]
    active: bool


@dataclass(frozen=True)
class RosterPreview:
    """Counts only: no roster value is part of a preview."""

    role_counts: tuple[tuple[str, int], ...]
    vertical_counts: tuple[tuple[int, int], ...]
    without_manager: int
    max_depth: int
    issue_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RosterReport:
    """The parsed rows and every issue found in one pass."""

    rows: tuple[RosterRow, ...]
    issues: tuple[RosterIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def preview(self) -> RosterPreview:
        role_counts = Counter(row.role for row in self.rows if row.role)
        vertical_counts = Counter(
            vertical_id
            for row in self.rows
            for vertical_id in row.vertical_ids
        )
        issue_counts = Counter(issue.code for issue in self.issues)
        return RosterPreview(
            role_counts=tuple(
                (role, role_counts[role])
                for role in VALID_ROLES
                if role_counts[role]
            ),
            vertical_counts=tuple(sorted(vertical_counts.items())),
            without_manager=sum(
                1
                for row in self.rows
                if row.reports_to_roll_number is None
            ),
            max_depth=_max_depth(self.rows),
            issue_counts=tuple(sorted(issue_counts.items())),
        )


def validate_roster(
    path: str | Path,
    *,
    known_vertical_ids: frozenset[int] | None = None,
    allow_fake_ids: bool = False,
) -> RosterReport:
    """Read and validate one CSV; never writes and never returns partial rows.

    ``known_vertical_ids`` is ``None`` when no database is available (the
    ``roster-check`` preview without ``--db``); the vertical id is then checked
    for form only. ``allow_fake_ids`` is set only by ``seed-fake``.
    """
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError as error:
        raise RosterError("the file could not be read") from error
    return _validate_rows(
        rows,
        known_vertical_ids=known_vertical_ids,
        allow_fake_ids=allow_fake_ids,
    )


def validate_roster_text(
    text: str,
    *,
    known_vertical_ids: frozenset[int] | None = None,
    allow_fake_ids: bool = False,
) -> RosterReport:
    """Validate CSV text through the same path as :func:`validate_roster`."""
    return _validate_rows(
        list(csv.reader(io.StringIO(text))),
        known_vertical_ids=known_vertical_ids,
        allow_fake_ids=allow_fake_ids,
    )


def _validate_rows(
    rows: list[list[str]],
    *,
    known_vertical_ids: frozenset[int] | None,
    allow_fake_ids: bool,
) -> RosterReport:
    if not rows:
        return RosterReport(rows=(), issues=(RosterIssue(1, "BAD_HEADER"),))
    header = tuple(cell.strip() for cell in rows[0])
    if header != ROSTER_COLUMNS:
        return RosterReport(rows=(), issues=(RosterIssue(1, "BAD_HEADER"),))

    issues: list[RosterIssue] = []
    parsed: list[RosterRow] = []
    seen_roll: set[str] = set()
    seen_phone: set[str] = set()
    seen_discord: set[str] = set()

    for offset, cells in enumerate(rows[1:], start=2):
        if len(cells) != len(ROSTER_COLUMNS):
            issues.append(RosterIssue(offset, "BAD_COLUMNS"))
            continue
        row = _parse_row(
            cells,
            offset,
            issues,
            known_vertical_ids=known_vertical_ids,
            allow_fake_ids=allow_fake_ids,
            seen_roll=seen_roll,
            seen_phone=seen_phone,
            seen_discord=seen_discord,
        )
        if row is not None:
            parsed.append(row)

    _check_managers(parsed, issues)
    return RosterReport(rows=tuple(parsed), issues=tuple(issues))


def _parse_row(
    cells: list[str],
    line: int,
    issues: list[RosterIssue],
    *,
    known_vertical_ids: frozenset[int] | None,
    allow_fake_ids: bool,
    seen_roll: set[str],
    seen_phone: set[str],
    seen_discord: set[str],
) -> RosterRow | None:
    roll_number = cells[0].strip()
    name = cells[1].strip()
    discord_user_id = cells[2].strip()
    phone = cells[3].strip()
    raw_role = cells[4].strip()
    manager = cells[5].strip()
    raw_verticals = cells[6].strip()
    raw_active = cells[7].strip()

    if not roll_number or not name or not phone or not raw_role:
        issues.append(RosterIssue(line, "EMPTY_FIELD"))

    role = _ROLE_BY_CASEFOLD.get(raw_role.casefold())
    if raw_role and role is None:
        issues.append(RosterIssue(line, "BAD_ROLE"))

    if raw_active in ("0", "1"):
        active = raw_active == "1"
    else:
        active = False
        issues.append(RosterIssue(line, "BAD_ACTIVE"))

    if discord_user_id == "":
        issues.append(RosterIssue(line, "MISSING_DISCORD"))
    elif not _valid_discord_id(discord_user_id, allow_fake_ids=allow_fake_ids):
        issues.append(RosterIssue(line, "BAD_DISCORD_ID"))

    if phone and not _valid_phone(phone):
        issues.append(RosterIssue(line, "BAD_PHONE"))

    vertical_ids, vertical_bad = _parse_verticals(raw_verticals)
    if vertical_bad or (
        known_vertical_ids is not None
        and any(
            vertical_id not in known_vertical_ids
            for vertical_id in vertical_ids
        )
    ):
        issues.append(RosterIssue(line, "BAD_VERTICAL"))

    if roll_number:
        if roll_number in seen_roll:
            issues.append(RosterIssue(line, "DUPLICATE_ROLL"))
        else:
            seen_roll.add(roll_number)
    if phone:
        if phone in seen_phone:
            issues.append(RosterIssue(line, "DUPLICATE_PHONE"))
        else:
            seen_phone.add(phone)
    if discord_user_id:
        if discord_user_id in seen_discord:
            issues.append(RosterIssue(line, "DUPLICATE_DISCORD"))
        else:
            seen_discord.add(discord_user_id)

    if not roll_number:
        return None
    return RosterRow(
        line=line,
        roll_number=roll_number,
        name=name,
        discord_user_id=discord_user_id,
        phone=phone,
        role=role or "",
        reports_to_roll_number=manager or None,
        vertical_ids=vertical_ids,
        active=active,
    )


def _valid_discord_id(value: str, *, allow_fake_ids: bool) -> bool:
    if (
        allow_fake_ids
        and value.startswith(FAKE_DISCORD_PREFIX)
        and len(value) > len(FAKE_DISCORD_PREFIX)
    ):
        return True
    return (
        value.isascii()
        and value.isdigit()
        and MIN_DISCORD_ID_LENGTH <= len(value) <= MAX_DISCORD_ID_LENGTH
    )


def _valid_phone(value: str) -> bool:
    if any(
        character not in _PHONE_PUNCTUATION and not character.isdigit()
        for character in value
    ):
        return False
    digits = sum(1 for character in value if character.isdigit())
    return MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS


def _parse_verticals(raw: str) -> tuple[tuple[int, ...], bool]:
    vertical_ids: list[int] = []
    bad = False
    for token in raw.split(";"):
        item = token.strip()
        if item == "":
            continue
        try:
            vertical_id = int(item)
        except ValueError:
            bad = True
            continue
        if vertical_id < 1:
            bad = True
            continue
        if vertical_id not in vertical_ids:
            vertical_ids.append(vertical_id)
    return tuple(vertical_ids), bad


def _check_managers(
    rows: list[RosterRow], issues: list[RosterIssue]
) -> None:
    by_roll: dict[str, RosterRow] = {}
    for row in rows:
        by_roll.setdefault(row.roll_number, row)

    edges: dict[str, str] = {}
    for row in rows:
        manager = row.reports_to_roll_number
        if manager is None:
            continue
        if manager == row.roll_number:
            issues.append(RosterIssue(row.line, "SELF_MANAGER"))
            continue
        if manager not in by_roll:
            issues.append(RosterIssue(row.line, "UNKNOWN_MANAGER"))
            continue
        edges[row.roll_number] = manager

    # Every node on a cycle is reported once, on its own CSV line.
    done: set[str] = set()
    reported: set[str] = set()
    for start in by_roll:
        if start in done:
            continue
        path: list[str] = []
        position: dict[str, int] = {}
        node: str | None = start
        while node is not None and node not in done:
            if node in position:
                break
            position[node] = len(path)
            path.append(node)
            node = edges.get(node)
        if node is not None and node in position:
            for member in path[position[node]:]:
                if member not in reported:
                    issues.append(
                        RosterIssue(by_roll[member].line, "REPORT_CYCLE")
                    )
                    reported.add(member)
        done.update(path)


def _max_depth(rows: tuple[RosterRow, ...]) -> int:
    """Longest chain of manager edges above a root (root = 0)."""
    by_roll: dict[str, RosterRow] = {}
    for row in rows:
        by_roll.setdefault(row.roll_number, row)
    memo: dict[str, int] = {}

    def depth(roll_number: str, seen: frozenset[str]) -> int:
        if roll_number in memo:
            return memo[roll_number]
        row = by_roll.get(roll_number)
        manager = None if row is None else row.reports_to_roll_number
        if manager is None or manager not in by_roll or manager in seen:
            memo[roll_number] = 0
            return 0
        value = 1 + depth(manager, seen | {roll_number})
        memo[roll_number] = value
        return value

    return max(
        (depth(roll_number, frozenset()) for roll_number in by_roll),
        default=0,
    )


def format_preview(report: RosterReport) -> str:
    """Render the counts-only preview; never a roster value."""
    preview = report.preview
    roles = " ".join(
        f"{role}={count}" for role, count in preview.role_counts
    ) or "none"
    verticals = " ".join(
        f"{vertical_id}={count}"
        for vertical_id, count in preview.vertical_counts
    ) or "none"
    issue_counts = " ".join(
        f"{code}={count}" for code, count in preview.issue_counts
    ) or "none"
    return "\n".join(
        (
            f"rows={len(report.rows)} issues={len(report.issues)}",
            f"role {roles}",
            f"vertical {verticals}",
            f"without_manager={preview.without_manager}",
            f"max_depth={preview.max_depth}",
            f"issue {issue_counts}",
        )
    )


def read_vertical_ids(connection: sqlite3.Connection) -> frozenset[int]:
    """The valid vertical ids in this database (empty before they are seeded)."""
    try:
        rows = connection.execute("SELECT id FROM vertical").fetchall()
    except sqlite3.Error as error:
        raise RosterError("the database could not be read") from error
    return frozenset(int(row["id"]) for row in rows)


def open_read_only(path: str | Path) -> sqlite3.Connection:
    """Open an existing database read-only, so a preview writes nothing."""
    location = Path(path)
    if not location.is_file():
        raise RosterError("the database does not exist")
    uri = location.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise RosterError("the database could not be opened") from error
    connection.row_factory = sqlite3.Row
    return connection


def require_outside_repository(path: str | Path, *, what: str) -> Path:
    """Refuse a roster or database path inside the repository."""
    resolved = Path(path).resolve()
    if resolved == _REPOSITORY_ROOT or resolved.is_relative_to(
        _REPOSITORY_ROOT
    ):
        raise RosterError(f"the {what} must be outside the repository")
    return resolved


def apply_roster(
    connection: sqlite3.Connection, rows: tuple[RosterRow, ...]
) -> int:
    """Insert every row in one transaction; refuse a non-empty person table.

    People are inserted first, then ``reports_to`` is resolved to the new ids,
    then memberships are added. Any failure rolls the whole load back, so a
    half-loaded roster is impossible.
    """
    if not rows:
        raise RosterError("the roster has no rows")
    with transaction(connection):
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM person"
        ).fetchone()
        if count is None or int(count["n"]) != 0:
            raise RosterError(
                "the person table is not empty; the MVP loads the roster once"
                " into a fresh database"
            )
        ids: dict[str, int] = {}
        for row in rows:
            cursor = connection.execute(
                "INSERT INTO person"
                " (name, discord_user_id, phone, role, reports_to, active,"
                " roll_number) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (
                    row.name,
                    row.discord_user_id,
                    row.phone,
                    row.role,
                    1 if row.active else 0,
                    row.roll_number,
                ),
            )
            ids[row.roll_number] = int(cursor.lastrowid or 0)
        for row in rows:
            manager = row.reports_to_roll_number
            if manager is None:
                continue
            connection.execute(
                "UPDATE person SET reports_to = ? WHERE id = ?",
                (ids[manager], ids[row.roll_number]),
            )
        for row in rows:
            for vertical_id in row.vertical_ids:
                connection.execute(
                    "INSERT INTO person_vertical (person_id, vertical_id)"
                    " VALUES (?, ?)",
                    (ids[row.roll_number], vertical_id),
                )
    return len(rows)


def build_fake_roster_csv(people: int = DEFAULT_FAKE_PEOPLE) -> str:
    """Build a deterministic, obviously fake roster as CSV text.

    One Admin, one or two Heads, two AHs in different verticals, active
    Volunteers reporting to an AH, and exactly one inactive person. Every roll
    number, name, phone and Discord id is synthetic.
    """
    if people < MIN_FAKE_PEOPLE:
        raise RosterError(
            f"--people must be at least {MIN_FAKE_PEOPLE}"
        )
    heads = 2 if people >= 8 else 1
    volunteers = people - 1 - heads - 2 - 1
    if volunteers < 1:  # pragma: no cover - guarded by MIN_FAKE_PEOPLE
        raise RosterError(f"--people must be at least {MIN_FAKE_PEOPLE}")

    rows: list[list[str]] = []
    index = 0

    def add(
        roll_number: str,
        name: str,
        role: str,
        manager: str,
        verticals: str,
        active: str,
    ) -> None:
        nonlocal index
        index += 1
        rows.append(
            [
                roll_number,
                name,
                f"{FAKE_DISCORD_PREFIX}{index:04d}",
                f"+1555010{index:04d}",
                role,
                manager,
                verticals,
                active,
            ]
        )

    add(
        "FAKE-ADMIN-01",
        "Fake Admin",
        "Admin",
        "",
        ";".join(str(vertical_id) for vertical_id, _ in FAKE_VERTICALS),
        "1",
    )
    for head in range(1, heads + 1):
        add(
            f"FAKE-HEAD-{head:02d}",
            f"Fake Head {head:02d}",
            "Head",
            "FAKE-ADMIN-01",
            str(head),
            "1",
        )
    for ah in (1, 2):
        manager = f"FAKE-HEAD-{min(ah, heads):02d}"
        add(
            f"FAKE-AH-{ah:02d}",
            f"Fake AH {ah:02d}",
            "AH",
            manager,
            str(ah),
            "1",
        )
    add(
        "FAKE-INACTIVE-01",
        "Fake Inactive 01",
        "Volunteer",
        "FAKE-AH-01",
        "1",
        "0",
    )
    for number in range(1, volunteers + 1):
        ah = 1 if number % 2 == 1 else 2
        add(
            f"FAKE-VOL-{number:02d}",
            f"Fake Volunteer {number:02d}",
            "Volunteer",
            f"FAKE-AH-{ah:02d}",
            str(ah),
            "1",
        )

    lines = [",".join(ROSTER_COLUMNS)]
    lines.extend(",".join(row) for row in rows)
    return "\n".join(lines) + "\n"


def seed_fake(
    database: str | Path, *, people: int = DEFAULT_FAKE_PEOPLE
) -> RosterReport:
    """Create a new database outside the repository and load a fake roster.

    Refuses an existing path, an in-repository path and an impossible people
    count before it creates anything. The CSV text goes through the same
    validator and insert path as a real load.
    """
    location = require_outside_repository(database, what="database")
    if location.exists():
        raise RosterError(
            "the database already exists; refusing to overwrite it"
        )
    text = build_fake_roster_csv(people)
    known = frozenset(vertical_id for vertical_id, _ in FAKE_VERTICALS)
    report = validate_roster_text(
        text, known_vertical_ids=known, allow_fake_ids=True
    )
    if not report.ok:
        raise RosterError("the generated fake roster did not validate")

    connection: sqlite3.Connection | None = None
    created = False
    try:
        connection = connect(location)
        created = True
        apply_migrations(connection)
        for vertical_id, name in FAKE_VERTICALS:
            connection.execute(
                "INSERT INTO vertical (id, name) VALUES (?, ?)",
                (vertical_id, name),
            )
        apply_roster(connection, report.rows)
        return report
    except BaseException:
        if connection is not None:
            connection.close()
            connection = None
        if created:
            location.unlink(missing_ok=True)
        raise
    finally:
        if connection is not None:
            connection.close()
