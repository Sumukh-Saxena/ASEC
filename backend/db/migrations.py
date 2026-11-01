"""Versioned SQL migration runner for ASEC (Slice 2.1).

The runner is deliberately small: it applies ``*.sql`` files from a directory, once
each, in numeric version order, and records each success in a ``schema_migrations``
tracking table. It is not a general-purpose migration framework.

Migration files are named ``<version>_<name>.sql`` (for example ``0001_create_x.sql``).
The numeric prefix is the version. Each successful migration is wrapped in a single
transaction together with its tracking row, so a failure leaves neither partial
schema changes nor a false success record. Checksums are verified for migrations that
were already applied, and files that changed, disappeared, or collide are reported as
errors instead of being applied.

Files listed in :data:`ATOMIC_WINDOWS` are applied as one transaction: either every
migration in the window lands or none does. A batch runs with ``PRAGMA foreign_keys =
OFF`` (set before ``BEGIN``, since SQLite ignores the pragma inside a transaction) so a
table rebuild can drop and rename a referenced table; the batch then runs ``PRAGMA
foreign_key_check`` before ``COMMIT`` and a violation aborts the whole window. The
caller's pragma value is restored in a ``finally`` block.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.db.connection import transaction

TRACKING_TABLE = "schema_migrations"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_FILENAME = re.compile(r"^(?P<version>\d+)_(?P<name>.+)\.sql$")


class MigrationError(Exception):
    """Raised when migrations are missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class Migration:
    """One migration file with its parsed version, SQL text and checksum."""

    version: int
    name: str
    sql: str
    checksum: str


# PB-3/PB-5/PB-6 rebuild the task, person and announcement tables together: the
# window is applied or rolled back as one unit because the new shapes depend on
# each other (and on the pre-roster window, owner decision O7-1).
ATOMIC_WINDOWS: tuple[frozenset[int], ...] = (frozenset({16, 17, 18}),)


def apply_migrations(
    connection: sqlite3.Connection, directory: str | Path = MIGRATIONS_DIR
) -> list[int]:
    """Apply pending migrations in ``directory`` and return the versions applied.

    Already-applied migrations are skipped after their recorded checksums are
    verified. Raises :class:`MigrationError` for an unreadable directory, an invalid
    or duplicate filename, a changed checksum, or a recorded migration whose file is
    gone.
    """
    migrations = _load_migrations(Path(directory))
    applied = _applied_checksums(connection)
    by_version = {migration.version: migration for migration in migrations}

    for version, checksum in sorted(applied.items()):
        current = by_version.get(version)
        if current is None:
            raise MigrationError(
                f"Applied migration {version} is missing from {directory}"
            )
        if current.checksum != checksum:
            raise MigrationError(
                f"Applied migration {version} ({current.name}) checksum changed"
            )

    executed: list[int] = []
    i = 0
    while i < len(migrations):
        migration = migrations[i]
        if migration.version in applied:
            i += 1
            continue

        window = next((w for w in ATOMIC_WINDOWS if migration.version in w), None)
        if window is not None:
            batch = [
                m
                for m in migrations
                if m.version in window and m.version not in applied
            ]
            _apply_batch(connection, batch)
            for applied_migration in batch:
                executed.append(applied_migration.version)
            while i < len(migrations) and migrations[i].version in window:
                i += 1
        else:
            _apply_batch(connection, [migration])
            executed.append(migration.version)
            i += 1
    return executed


def _load_migrations(directory: Path) -> list[Migration]:
    if not directory.is_dir():
        raise MigrationError(f"Migrations directory not found: {directory}")

    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"Invalid migration filename (expected <version>_<name>.sql): {path.name}"
            )
        version = int(match.group("version"))
        if version in seen:
            raise MigrationError(
                f"Duplicate migration version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        data = path.read_bytes()
        try:
            sql = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationError(f"Migration file is not valid UTF-8: {path.name}") from error
        migrations.append(
            Migration(
                version=version,
                name=path.stem,
                sql=sql,
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    return migrations


def _applied_checksums(connection: sqlite3.Connection) -> dict[int, str]:
    _ensure_tracking_table(connection)
    rows = connection.execute(
        f"SELECT version, checksum FROM {TRACKING_TABLE}"
    ).fetchall()
    return {int(row["version"]): str(row["checksum"]) for row in rows}


def _ensure_tracking_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} ("
        "version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )


def _apply_batch(
    connection: sqlite3.Connection, migrations: list[Migration]
) -> None:
    saved_fk = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        with transaction(connection):
            for migration in migrations:
                statements = _split_statements(migration.sql)
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    f"INSERT INTO {TRACKING_TABLE} (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, _utc_now()),
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                names = ", ".join(f"{m.version} ({m.name})" for m in migrations)
                raise MigrationError(
                    f"Migration batch {names} failed foreign_key_check: {violations}"
                )
    finally:
        if saved_fk:
            connection.execute("PRAGMA foreign_keys = ON")
        else:
            connection.execute("PRAGMA foreign_keys = OFF")


def _split_statements(script: str) -> list[str]:
    """Split a migration script into executable statements.

    Statements are accumulated until ``sqlite3.complete_statement`` reports a
    complete statement, then executed one at a time so a failure can roll back the
    whole migration. Comment-only fragments are skipped.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if _has_sql(buffer):
                statements.append(buffer.strip())
            buffer = ""
    if _has_sql(buffer):
        statements.append(buffer.strip())
    return statements


def _has_sql(statement: str) -> bool:
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return True
    return False


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
