"""W9 H7: ``python -m backend import-groups`` (FR-ING-09 group mapping).

The CSV maps Discord channel ids to vertical ids. Validation is all-or-nothing
in one transaction, a conflicting mapping changes nothing, and a re-import is
idempotent. Output carries counts only: never channel ids, vertical names or
file content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.__main__ import PRODUCTION_DB_PATH_KEY, main
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations

CHANNEL_A = "12345678901234567"
CHANNEL_B = "22345678901234567"
CHANNEL_C = "32345678901234567"


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "import.db"
    connection = connect(path)
    try:
        apply_migrations(connection)
        connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'Logistics')")
        connection.execute("INSERT INTO vertical (id, name) VALUES (2, 'Food')")
    finally:
        connection.close()
    return path


def _csv(tmp_path: Path, body: str, name: str = "groups.csv") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _run(
    database: Path,
    csv_path: Path,
    *,
    config: Config | None = None,
    confirm: bool = False,
) -> tuple[int, str, str]:
    args = ["import-groups", "--db", str(database), "--file", str(csv_path)]
    if confirm:
        args.append("--confirm")
    captured = _Captured()
    import contextlib

    with contextlib.redirect_stdout(captured.stdout), contextlib.redirect_stderr(
        captured.stderr
    ):
        code = main(args, config=Config({}) if config is None else config)
    return code, captured.stdout.getvalue(), captured.stderr.getvalue()


class _Captured:
    def __init__(self) -> None:
        import io

        self.stdout = io.StringIO()
        self.stderr = io.StringIO()


def _rows(database: Path) -> list[tuple[str, int | None]]:
    connection = connect(database)
    try:
        return [
            (str(row["discord_channel_id"]), row["vertical_id"])
            for row in connection.execute(
                "SELECT discord_channel_id, vertical_id FROM source_group"
                " ORDER BY discord_channel_id"
            )
        ]
    finally:
        connection.close()


def test_import_adds_rows_and_reports_counts_only(tmp_path: Path) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path,
        "discord_channel_id,vertical_id\n"
        f"{CHANNEL_A},1\n"
        f"{CHANNEL_B},\n",
    )

    code, out, err = _run(database, csv_path)

    assert code == 0
    assert "added=2" in out
    assert "unchanged=0" in out
    assert "conflicts=0" in out
    assert CHANNEL_A not in out
    assert CHANNEL_A not in err
    assert _rows(database) == [(CHANNEL_A, 1), (CHANNEL_B, None)]


def test_reimport_is_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path, "discord_channel_id,vertical_id\n" f"{CHANNEL_A},1\n"
    )
    assert _run(database, csv_path)[0] == 0

    code, out, _err = _run(database, csv_path)

    assert code == 0
    assert "added=0" in out
    assert "unchanged=1" in out
    assert "conflicts=0" in out
    assert _rows(database) == [(CHANNEL_A, 1)]


def test_conflict_changes_no_rows_and_exits_two(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = _csv(
        tmp_path, "discord_channel_id,vertical_id\n" f"{CHANNEL_A},1\n", "first.csv"
    )
    assert _run(database, first)[0] == 0
    conflicting = _csv(
        tmp_path,
        "discord_channel_id,vertical_id\n"
        f"{CHANNEL_B},2\n"
        f"{CHANNEL_A},2\n",
        "conflict.csv",
    )

    code, out, err = _run(database, conflicting)

    assert code == 2
    assert "added=0" in out
    assert "conflicts=1" in out
    assert CHANNEL_A not in out + err
    # All-or-nothing: the new, valid row was not inserted either.
    assert _rows(database) == [(CHANNEL_A, 1)]


@pytest.mark.parametrize(
    "body",
    [
        "discord_channel_id,vertical_id\n123,1\n",
        "discord_channel_id,vertical_id\n" + "1" * 21 + ",1\n",
        "discord_channel_id,vertical_id\n" + CHANNEL_A + ",1\n" + CHANNEL_A + ",2\n",
        "discord_channel_id,vertical_id\n" + CHANNEL_A + ",99\n",
        "channel,vertical\n" + CHANNEL_A + ",1\n",
    ],
)
def test_malformed_file_is_rejected_without_writes(
    tmp_path: Path, body: str
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, body)

    code, out, err = _run(database, csv_path)

    assert code == 2
    assert "import-groups failed" in err
    assert CHANNEL_A not in out + err
    assert _rows(database) == []


def test_production_database_path_is_refused_without_confirm(tmp_path: Path) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path, "discord_channel_id,vertical_id\n" f"{CHANNEL_A},1\n"
    )
    config = Config({PRODUCTION_DB_PATH_KEY: str(database)})

    code, out, err = _run(database, csv_path, config=config)

    assert code == 2
    assert "production" in err.lower()
    assert CHANNEL_A not in out + err
    assert _rows(database) == []


def test_production_database_path_is_allowed_with_confirm(tmp_path: Path) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path, "discord_channel_id,vertical_id\n" f"{CHANNEL_A},1\n"
    )
    config = Config({PRODUCTION_DB_PATH_KEY: str(database)})

    code, out, _err = _run(database, csv_path, config=config, confirm=True)

    assert code == 0
    assert "added=1" in out
    assert _rows(database) == [(CHANNEL_A, 1)]
