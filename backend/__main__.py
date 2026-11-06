"""Backend entrypoint: version flag, the Discord smoke and the live runner.

The smoke command is a manual, read-only verification step (owner addition 4
to Phase 9 W5). It reads one page of recent Discord messages, ingests them
through the normal :func:`backend.ingest.ingest_message` path into a scratch
database, and prints counts only — never message content, Discord user ids or
the bot token. It refuses to run against the configured production database
path (:data:`PRODUCTION_DB_PATH_KEY`).

``run-live`` (W8b A3) is the process boundary that drives the W8a live tick:
it reads the token through :meth:`Config.require`, refuses the production path
as a ``--db`` argument and any database already locked to REPLAY, checks the
Message Content readiness once, and then polls at the configured interval
until interrupted. It installs no scheduler and prints counts only.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from backend import __version__
from backend.alerts import LogAlertSender
from backend.config import (
    DISCORD_BOT_TOKEN_KEY,
    Config,
    ConfigError,
    load_config,
    load_discord_settings,
    load_sqlite_settings,
    load_worker_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.discord_adapter import (
    DiscordAdapterError,
    DiscordRestSource,
    Transport,
    check_live_readiness,
    check_live_start,
)
from backend.group_import import GroupImportError, import_groups_from_file
from backend.ingest import ingest_message
from backend.logging_setup import setup_logging
from backend.worker import LiveTickResult, run_live_tick

logger = logging.getLogger("asec.cli")

#: The single documented production database path. ``discord-smoke`` and
#: ``run-live`` refuse a ``--db`` that resolves to it; ``run-live`` without
#: ``--db`` uses it.
PRODUCTION_DB_PATH_KEY = "ASEC_DB_PATH"

_USAGE = (
    "usage: python -m backend discord-smoke --channel-id <id>"
    " [--db <scratch path>]"
)

_LIVE_USAGE = (
    "usage: python -m backend run-live [--db <database path>]"
    " [--interval-seconds <positive number>] [--once]"
)

_IMPORT_GROUPS_USAGE = (
    "usage: python -m backend import-groups --db <database path>"
    " --file <csv path> [--confirm]"
)


def _parse_smoke_args(args: Sequence[str]) -> tuple[str, str] | None:
    channel_id: str | None = None
    database = ":memory:"
    index = 0
    while index < len(args):
        option = args[index]
        if option in ("--channel-id", "--db"):
            if index + 1 >= len(args):
                return None
            value = args[index + 1].strip()
            if value == "":
                return None
            if option == "--channel-id":
                channel_id = value
            else:
                database = value
            index += 2
            continue
        return None
    if channel_id is None:
        return None
    return channel_id, database


def _is_production_path(config: Config, database: str) -> bool:
    if database == ":memory:":
        return False
    production = config.get(PRODUCTION_DB_PATH_KEY)
    if production is None or production.strip() == "":
        return False
    return Path(database).resolve() == Path(production.strip()).resolve()


def _run_discord_smoke(
    args: Sequence[str], *, config: Config, transport: Transport | None
) -> int:
    parsed = _parse_smoke_args(args)
    if parsed is None:
        print(_USAGE, file=sys.stderr)
        return 2
    channel_id, database = parsed
    if _is_production_path(config, database):
        print(
            "refusing to run discord-smoke against the production database path"
            f" configured in {PRODUCTION_DB_PATH_KEY}",
            file=sys.stderr,
        )
        return 2
    try:
        setup_logging(config)
        token = config.require(DISCORD_BOT_TOKEN_KEY)
        settings = load_discord_settings(config)
        sqlite_settings = load_sqlite_settings(config)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2

    source = DiscordRestSource(token=token, transport=transport, settings=settings)
    connection = connect(database, busy_timeout_ms=sqlite_settings.busy_timeout_ms)
    try:
        apply_migrations(connection)
        result = source.poll_latest_page(
            channel_id=channel_id, sender=LogAlertSender()
        )
        check_live_readiness(result.latest_page, channel_id=channel_id)
        stored = 0
        duplicates = 0
        for event in result.messages:
            outcome = ingest_message(connection, event)
            if outcome.created:
                stored += 1
            else:
                duplicates += 1
    except (DiscordAdapterError, sqlite3.Error) as error:
        print(f"discord-smoke failed: {error}", file=sys.stderr)
        return 2
    finally:
        connection.close()

    print(
        f"fetched={result.fetched} stored={stored} duplicates={duplicates}"
        f" empty_content={result.empty_content}"
        f" with_mentions={result.with_mentions}"
        f" with_reply={result.with_reply}"
        f" skipped_system={result.skipped_system}"
        f" skipped_bot={result.skipped_bot}"
        f" skipped_sticker={result.skipped_sticker}"
    )
    return 0


def _parse_live_args(
    args: Sequence[str],
) -> tuple[str | None, float | None, bool] | None:
    database: str | None = None
    interval: float | None = None
    once = False
    index = 0
    while index < len(args):
        option = args[index]
        if option in ("--db", "--interval-seconds"):
            if index + 1 >= len(args):
                return None
            value = args[index + 1].strip()
            if value == "":
                return None
            if option == "--db":
                database = value
            else:
                try:
                    parsed = float(value)
                except ValueError:
                    return None
                if parsed <= 0:
                    return None
                interval = parsed
            index += 2
            continue
        if option == "--once":
            once = True
            index += 1
            continue
        return None
    return database, interval, once


def _has_replay_clock(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM source_group_clock WHERE mode = 'REPLAY' LIMIT 1"
    ).fetchone()
    return row is not None


def _mapped_channels(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = connection.execute(
        "SELECT id, discord_channel_id FROM source_group ORDER BY id"
    ).fetchall()
    return [(int(row["id"]), str(row["discord_channel_id"])) for row in rows]


def _print_live_counts(result: LiveTickResult) -> None:
    """Print one cycle's counts only (never content, user ids or the token)."""
    fetched = sum(poll.fetched for poll in result.polls)
    stored = sum(poll.stored for poll in result.polls)
    duplicates = sum(poll.duplicates for poll in result.polls)
    failed = sum(1 for poll in result.polls if poll.failed)
    skipped_replay = sum(1 for poll in result.polls if poll.skipped_replay)
    assigned = result.tick.assigned
    print(
        f"fetched={fetched} stored={stored} duplicates={duplicates}"
        f" failed={failed} skipped_replay={skipped_replay} assigned={assigned}"
    )
    logger.info(
        "run-live tick fetched=%s stored=%s duplicates=%s failed=%s"
        " skipped_replay=%s assigned=%s",
        fetched,
        stored,
        duplicates,
        failed,
        skipped_replay,
        assigned,
    )


def _run_live(
    args: Sequence[str],
    *,
    config: Config,
    transport: Transport | None,
    sleep: Callable[[float], None],
    now: Callable[[], str],
) -> int:
    parsed = _parse_live_args(args)
    if parsed is None:
        print(_LIVE_USAGE, file=sys.stderr)
        return 2
    database_arg, interval_arg, once = parsed
    if database_arg is not None:
        if _is_production_path(config, database_arg):
            print(
                "refusing to run run-live against the production database path"
                f" configured in {PRODUCTION_DB_PATH_KEY}",
                file=sys.stderr,
            )
            return 2
        database = database_arg
    else:
        production = config.get(PRODUCTION_DB_PATH_KEY)
        if production is None or production.strip() == "":
            print(
                f"{PRODUCTION_DB_PATH_KEY} is not set; pass --db or configure the"
                " production database path",
                file=sys.stderr,
            )
            return 2
        database = production.strip()
    try:
        setup_logging(config)
        token = config.require(DISCORD_BOT_TOKEN_KEY)
        discord_settings = load_discord_settings(config)
        worker_settings = load_worker_settings(config)
        sqlite_settings = load_sqlite_settings(config)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    interval = (
        worker_settings.interval_seconds if interval_arg is None else interval_arg
    )
    source = DiscordRestSource(
        token=token, transport=transport, settings=discord_settings
    )
    connection = connect(database, busy_timeout_ms=sqlite_settings.busy_timeout_ms)
    try:
        apply_migrations(connection)
        if _has_replay_clock(connection):
            print(
                "refusing to run run-live against a database in REPLAY mode",
                file=sys.stderr,
            )
            return 2
        mapped = _mapped_channels(connection)
        if not mapped:
            print(
                "warning: source_groups=0; no channels to poll",
                file=sys.stderr,
            )
        for _group_id, channel_id in mapped:
            check_live_start(source, channel_id=channel_id)
        failures: dict[int, int] = {}
        sender = LogAlertSender()

        def run_cycle() -> LiveTickResult:
            return run_live_tick(
                connection,
                source=source,
                now=now,
                alert_sender=sender,
                poll_safety_margin_seconds=(
                    worker_settings.poll_safety_margin_seconds
                ),
                poll_alert_after_failures=(
                    worker_settings.poll_alert_after_failures
                ),
                consecutive_failures=failures,
            )

        if once:
            _print_live_counts(run_cycle())
            return 0
        while True:
            _print_live_counts(run_cycle())
            sleep(interval)
    except KeyboardInterrupt:
        return 0
    except (DiscordAdapterError, sqlite3.Error) as error:
        print(f"run-live failed: {error}", file=sys.stderr)
        return 2
    finally:
        connection.close()


def _parse_import_groups_args(
    args: Sequence[str],
) -> tuple[str, str, bool] | None:
    database: str | None = None
    source_file: str | None = None
    confirm = False
    index = 0
    while index < len(args):
        option = args[index]
        if option in ("--db", "--file"):
            if index + 1 >= len(args):
                return None
            value = args[index + 1].strip()
            if value == "":
                return None
            if option == "--db":
                database = value
            else:
                source_file = value
            index += 2
            continue
        if option == "--confirm":
            confirm = True
            index += 1
            continue
        return None
    if database is None or source_file is None:
        return None
    return database, source_file, confirm


def _run_import_groups(args: Sequence[str], *, config: Config) -> int:
    parsed = _parse_import_groups_args(args)
    if parsed is None:
        print(_IMPORT_GROUPS_USAGE, file=sys.stderr)
        return 2
    database, source_file, confirm = parsed
    if not confirm and _is_production_path(config, database):
        print(
            "refusing to run import-groups against the production database path"
            f" configured in {PRODUCTION_DB_PATH_KEY} without --confirm",
            file=sys.stderr,
        )
        return 2
    try:
        setup_logging(config)
        sqlite_settings = load_sqlite_settings(config)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2

    connection = connect(database, busy_timeout_ms=sqlite_settings.busy_timeout_ms)
    try:
        apply_migrations(connection)
        counts = import_groups_from_file(connection, Path(source_file))
    except (GroupImportError, OSError, sqlite3.Error) as error:
        print(f"import-groups failed: {error}", file=sys.stderr)
        return 2
    finally:
        connection.close()

    print(
        f"added={counts.added} unchanged={counts.unchanged}"
        f" conflicts={counts.conflicts}"
    )
    return 2 if counts.conflicts else 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def main(
    argv: Sequence[str] | None = None,
    *,
    config: Config | None = None,
    transport: Transport | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(__version__)
        return 0
    if args and args[0] == "discord-smoke":
        return _run_discord_smoke(
            args[1:],
            config=load_config() if config is None else config,
            transport=transport,
        )
    if args and args[0] == "run-live":
        return _run_live(
            args[1:],
            config=load_config() if config is None else config,
            transport=transport,
            sleep=time.sleep if sleep is None else sleep,
            now=_utc_now if now is None else now,
        )
    if args and args[0] == "import-groups":
        return _run_import_groups(
            args[1:], config=load_config() if config is None else config
        )
    print(
        "ASEC backend (Phase 0). Use --version, 'discord-smoke' or 'run-live'.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
