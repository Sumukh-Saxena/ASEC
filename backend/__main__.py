"""Backend entrypoint: version flag and the owner-authorized Discord smoke.

The smoke command is a manual, read-only verification step (owner addition 4
to Phase 9 W5). It reads one page of recent Discord messages, ingests them
through the normal :func:`backend.ingest.ingest_message` path into a scratch
database, and prints counts only — never message content, Discord user ids or
the bot token. It refuses to run against the configured production database
path (:data:`PRODUCTION_DB_PATH_KEY`).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence

from backend import __version__
from backend.alerts import LogAlertSender
from backend.config import (
    DISCORD_BOT_TOKEN_KEY,
    Config,
    ConfigError,
    load_config,
    load_discord_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.discord_adapter import (
    DiscordAdapterError,
    DiscordRestSource,
    Transport,
    check_live_readiness,
)
from backend.ingest import ingest_message

#: Environment variable naming the production database file. The smoke command
#: refuses to touch this file, so a manual test can never write live data.
PRODUCTION_DB_PATH_KEY = "ASEC_DB_PATH"

_USAGE = (
    "usage: python -m backend discord-smoke --channel-id <id>"
    " [--db <scratch path>]"
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
    from pathlib import Path

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
        token = config.require(DISCORD_BOT_TOKEN_KEY)
        settings = load_discord_settings(config)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2

    source = DiscordRestSource(token=token, transport=transport, settings=settings)
    connection = connect(database)
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
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    config: Config | None = None,
    transport: Transport | None = None,
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
    print(
        "ASEC backend (Phase 0). Use --version or 'discord-smoke'.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
