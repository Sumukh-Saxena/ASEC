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

import json
import logging
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend import __version__
from backend.alerts import LogAlertSender
from backend.config import (
    API_ALLOW_NON_LOOPBACK_KEY,
    DISCORD_BOT_TOKEN_KEY,
    Config,
    ConfigError,
    load_api_settings,
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
from backend.export import ExportError, export_database
from backend.group_import import GroupImportError, import_groups_from_file
from backend.ingest import ingest_message
from backend.logging_setup import setup_logging
from backend.notification_delivery import OtpSender
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

_SERVE_API_USAGE = (
    "usage: python -m backend serve-api [--host <host>] [--port <port>]"
)

_EXPORT_USAGE = (
    "usage: python -m backend export --db <database path>"
    " --destination <directory>"
    " [--unmasked --i-understand-this-contains-real-data]"
)

#: The explicit acknowledgement an unmasked export requires (owner decision D).
_UNMASKED_ACK = "--i-understand-this-contains-real-data"

_OPENAPI_USAGE = "usage: python -m backend openapi [--check]"

#: The checked-in OpenAPI document, relative to the repository root.
_OPENAPI_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"
)

#: A fixed instant for the dummy app; the document build never reads a clock.
_OPENAPI_NOW = "1970-01-01T00:00:00+00:00"

#: The one optional mutation header, documented next to the error table.
_IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Only these literals are treated as loopback; anything else needs the
#: explicit API_ALLOW_NON_LOOPBACK flag (W2, O12-6).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _openapi_connection_factory() -> sqlite3.Connection:
    """A factory that must never be called: the document needs no database."""
    raise RuntimeError("the OpenAPI document is built without a database")


def _build_openapi_spec() -> dict[str, Any]:
    """Build the real app with dummy settings and return its OpenAPI document.

    No database connection, network call, environment value, secret or bound
    port is involved: the connection factory raises if anything tries to use
    it, the clock is a fixed instant, and the configuration is empty (so every
    setting takes its documented default).
    """
    from backend.api import create_app
    from backend.api.errors import ERROR_MESSAGES

    app = create_app(
        connection_factory=_openapi_connection_factory,
        now=lambda: _OPENAPI_NOW,
        config=Config({}),
        otp_sender=None,
    )
    spec: dict[str, Any] = app.openapi()
    components = spec.setdefault("components", {})
    parameters = components.setdefault("parameters", {})
    parameters["IdempotencyKey"] = {
        "name": _IDEMPOTENCY_HEADER,
        "in": "header",
        "required": False,
        "description": (
            "Optional client key for a mutating request; a replay of the same"
            " key returns the stored response."
        ),
        "schema": {"type": "string", "minLength": 16, "maxLength": 64},
    }
    schemas = components.setdefault("schemas", {})
    schemas["ErrorResponse"] = {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message", "request_id"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "request_id": {"type": "string"},
                },
            }
        },
    }
    spec["x-asec-idempotency-key"] = {
        "header": _IDEMPOTENCY_HEADER,
        "component": "#/components/parameters/IdempotencyKey",
    }
    spec["x-asec-error-codes"] = {
        code: {"status": status, "message": message}
        for code, (status, message) in sorted(ERROR_MESSAGES.items())
    }
    return spec


def _render_openapi(spec: dict[str, Any]) -> str:
    """Render deterministically: sorted keys, two-space indent, LF, one newline."""
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _run_openapi(args: Sequence[str], *, path: Path | None = None) -> int:
    """Write the document, or check it with ``--check`` and never write on drift."""
    check = False
    if args == ["--check"]:
        check = True
    elif args:
        print(_OPENAPI_USAGE, file=sys.stderr)
        return 2
    from backend.api import ApiConfigurationError

    try:
        spec = _build_openapi_spec()
    except ApiConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    rendered = _render_openapi(spec)
    destination = _OPENAPI_PATH if path is None else path
    if check:
        try:
            with destination.open("r", encoding="utf-8", newline="") as handle:
                existing = handle.read()
        except OSError:
            print(f"openapi drift: {destination} is missing", file=sys.stderr)
            return 1
        if existing != rendered:
            print(f"openapi drift: {destination} is out of date", file=sys.stderr)
            return 1
        print("openapi: up to date")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8", newline="")
    print(f"openapi: wrote {destination}")
    return 0


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


def _parse_serve_api_args(
    args: Sequence[str],
) -> tuple[str | None, int | None] | None:
    host: str | None = None
    port: int | None = None
    index = 0
    while index < len(args):
        option = args[index]
        if option in ("--host", "--port"):
            if index + 1 >= len(args):
                return None
            value = args[index + 1].strip()
            if value == "":
                return None
            if option == "--host":
                host = value
            else:
                try:
                    parsed = int(value)
                except ValueError:
                    return None
                if not 1 <= parsed <= 65535:
                    return None
                port = parsed
            index += 2
            continue
        return None
    return host, port


def _configured_otp_sender(config: Config) -> OtpSender | None:
    """Build the production OTP DM sender, or report that none is configured.

    The token is read only through :meth:`Config.require`, which the sender
    calls; nothing here prints or returns it. Without a token, sign-in still
    answers normally and the private ledger records SENDER_UNCONFIGURED.
    """
    token = config.get(DISCORD_BOT_TOKEN_KEY)
    if token is None or token.strip() == "":
        print(
            "warning: no OTP sender configured; sign-in requests will not"
            " deliver a DM",
            file=sys.stderr,
        )
        return None
    from backend.discord_adapter import UrllibTransport
    from backend.notification_delivery import DiscordDmOtpSender

    return DiscordDmOtpSender(config=config, transport=UrllibTransport())


def _serve_with_uvicorn(app: object, host: str, port: int) -> None:
    from backend.api.server import run_server

    run_server(app, host=host, port=port)  # type: ignore[arg-type]


def _run_serve_api(
    args: Sequence[str],
    *,
    config: Config,
    serve: Callable[[object, str, int], None],
) -> int:
    """Serve the API without migrations, a worker or any printed secret."""
    parsed = _parse_serve_api_args(args)
    if parsed is None:
        print(_SERVE_API_USAGE, file=sys.stderr)
        return 2
    host_arg, port_arg = parsed
    try:
        setup_logging(config)
        api_settings = load_api_settings(config)
        sqlite_settings = load_sqlite_settings(config)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    host = api_settings.host if host_arg is None else host_arg
    port = api_settings.port if port_arg is None else port_arg
    if host not in _LOOPBACK_HOSTS and not api_settings.allow_non_loopback:
        print(
            f"refusing to bind {host}: set {API_ALLOW_NON_LOOPBACK_KEY} to allow"
            " a non-loopback address",
            file=sys.stderr,
        )
        return 2
    production = config.get(PRODUCTION_DB_PATH_KEY)
    if production is None or production.strip() == "":
        print(
            f"{PRODUCTION_DB_PATH_KEY} is not set; serve-api needs the database"
            " path",
            file=sys.stderr,
        )
        return 2
    database = production.strip()
    if not Path(database).is_file():
        print(
            "database does not exist; apply migrations before serve-api",
            file=sys.stderr,
        )
        return 2

    def open_connection() -> sqlite3.Connection:
        return connect(
            database, busy_timeout_ms=sqlite_settings.busy_timeout_ms
        )

    try:
        from backend.api import ApiConfigurationError, create_app

        app = create_app(
            connection_factory=open_connection,
            now=_utc_now,
            config=config,
            otp_sender=_configured_otp_sender(config),
        )
    except (ConfigError, ApiConfigurationError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"serving API on {host}:{port}")
    try:
        serve(app, host, port)
    except KeyboardInterrupt:
        return 0
    return 0


def _parse_export_args(args: Sequence[str]) -> tuple[str, str, bool] | None:
    database: str | None = None
    destination: str | None = None
    unmasked = False
    acknowledged = False
    index = 0
    while index < len(args):
        option = args[index]
        if option in ("--db", "--destination"):
            if index + 1 >= len(args):
                return None
            value = args[index + 1].strip()
            if value == "":
                return None
            if option == "--db":
                database = value
            else:
                destination = value
            index += 2
            continue
        if option == "--unmasked":
            unmasked = True
            index += 1
            continue
        if option == _UNMASKED_ACK:
            acknowledged = True
            index += 1
            continue
        return None
    if database is None or destination is None:
        return None
    if unmasked != acknowledged:
        return None
    return database, destination, not unmasked


def _run_export(args: Sequence[str], *, now: str) -> int:
    parsed = _parse_export_args(args)
    if parsed is None:
        print(_EXPORT_USAGE, file=sys.stderr)
        return 2
    database, destination, masked = parsed
    if not masked:
        print(
            "warning: unmasked export contains real personal data;"
            " keep the destination private",
            file=sys.stderr,
        )
    try:
        result = export_database(
            database,
            destination=Path(destination),
            masked=masked,
            exported_at=now,
        )
    except (ExportError, OSError, sqlite3.Error) as error:
        print(f"export failed: {error}", file=sys.stderr)
        return 1
    print(
        f"masked={result.masked} exported_at={result.exported_at}"
        f" destination={result.destination}"
    )
    for name, count in result.datasets:
        print(f"{name}: {count}")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    config: Config | None = None,
    transport: Transport | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], str] | None = None,
    serve_api: Callable[[object, str, int], None] | None = None,
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
    if args and args[0] == "serve-api":
        return _run_serve_api(
            args[1:],
            config=load_config() if config is None else config,
            serve=_serve_with_uvicorn if serve_api is None else serve_api,
        )
    if args and args[0] == "export":
        return _run_export(
            args[1:], now=(_utc_now if now is None else now)()
        )
    if args and args[0] == "openapi":
        return _run_openapi(args[1:])
    print(
        "ASEC backend (Phase 0). Use --version, 'discord-smoke', 'run-live',"
        " 'import-groups', 'serve-api', 'export' or 'openapi'.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
