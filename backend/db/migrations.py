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

There is no application schema in this slice; the first real migration arrives with
the slice that introduces its tables.
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
    for migration in migrations:
        if migration.version in applied:
            continue
        _apply_one(connection, migration)
        executed.append(migration.version)
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


def _apply_one(connection: sqlite3.Connection, migration: Migration) -> None:
    statements = _split_statements(migration.sql)
    with transaction(connection):
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            f"INSERT INTO {TRACKING_TABLE} (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (migration.version, migration.name, migration.checksum, _utc_now()),
        )


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
