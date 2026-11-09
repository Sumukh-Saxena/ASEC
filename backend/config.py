"""Environment-based configuration for ASEC (Phase 0 foundation).

Values are read from the process environment, not from committed files. Diagnostic
output (``repr``, ``str`` and :meth:`Config.as_public_dict`) exposes configuration
key names only and never a configured value, so a config object is safe to log.
Actual values are available only through :meth:`Config.get` and
:meth:`Config.require`. This is a foundation only; it does not load ``.env`` or
implement any domain configuration.
"""

from __future__ import annotations

import calendar
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

_SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PWD",
    "KEY",
    "DSN",
    "PHONE",
    "OTP",
)

_REDACTED = "<redacted>"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def is_secret_key(name: str) -> bool:
    """Return True when an environment variable name looks like a secret."""
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


class Config:
    """Read-only view over a mapping of configuration values.

    Diagnostic output (``repr``, ``str``, :meth:`as_public_dict`) reports key names
    only. Values are exposed solely by :meth:`get` and :meth:`require`, so callers
    decide what is safe to reveal.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values: dict[str, str] = dict(values)

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name, default)

    def require(self, name: str) -> str:
        value = self._values.get(name)
        if value is None or value.strip() == "":
            raise ConfigError(f"Required configuration '{name}' is missing or empty")
        return value

    def as_public_dict(self) -> dict[str, str]:
        """Return the configuration key names with every value redacted.

        No configured value is ever included, regardless of the key's name, so the
        result is safe for logging and diagnostics.
        """
        return {key: _REDACTED for key in self._values}

    def __repr__(self) -> str:
        return f"Config({self.as_public_dict()!r})"

    __str__ = __repr__


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from the given mapping (defaults to ``os.environ``)."""
    source = os.environ if environ is None else environ
    return Config(dict(source))


#: W8a owner decision: a successful live poll authorizes the group clock only
#: to the poll instant minus this safety margin, so closure never runs ahead of
#: a page that has not been fetched and committed yet.
DEFAULT_POLL_SAFETY_MARGIN_SECONDS = 2.0

#: W8b owner decision: consecutive failed polls for one group before an alert
#: is raised. The first failures are retries; repeated failures need an
#: operator. The value is configuration (A10), never a call-site constant.
DEFAULT_POLL_ALERT_AFTER_FAILURES = 5


@dataclass(frozen=True)
class WorkerSettings:
    """Validated worker-loop timing (O9-3, NFR-MNT-01).

    Defaults are the owner-approved values: a 2 s interval with a 1-60 s
    exponential backoff after a failed tick. ``poll_safety_margin_seconds``
    (W8a) is the delay between a successful poll instant and the clock
    boundary it authorizes; it may be zero but never negative.
    ``poll_alert_after_failures`` (W8b) is the consecutive-failure threshold
    for one group's poll alert. Every value is configuration, never a
    hard-coded constant at the call site.
    """

    interval_seconds: float = 2.0
    backoff_min_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    poll_safety_margin_seconds: float = DEFAULT_POLL_SAFETY_MARGIN_SECONDS
    poll_alert_after_failures: int = DEFAULT_POLL_ALERT_AFTER_FAILURES


#: Configuration keys read by :func:`load_worker_settings`.
WORKER_INTERVAL_KEY = "WORKER_INTERVAL_SECONDS"
WORKER_BACKOFF_MIN_KEY = "WORKER_BACKOFF_MIN_SECONDS"
WORKER_BACKOFF_MAX_KEY = "WORKER_BACKOFF_MAX_SECONDS"
POLL_SAFETY_MARGIN_KEY = "POLL_SAFETY_MARGIN_SECONDS"
POLL_ALERT_AFTER_FAILURES_KEY = "POLL_ALERT_AFTER_FAILURES"


def load_worker_settings(config: Config) -> WorkerSettings:
    """Build :class:`WorkerSettings` from optional configuration overrides.

    Absent or blank values keep the O9-3 defaults. A value that is not a
    positive number, or a maximum backoff below the minimum, raises
    :class:`ConfigError` before the worker starts — invalid timing is never
    clamped silently.
    """
    defaults = WorkerSettings()
    interval = _positive_float(
        config, WORKER_INTERVAL_KEY, defaults.interval_seconds
    )
    backoff_min = _positive_float(
        config, WORKER_BACKOFF_MIN_KEY, defaults.backoff_min_seconds
    )
    backoff_max = _positive_float(
        config, WORKER_BACKOFF_MAX_KEY, defaults.backoff_max_seconds
    )
    if backoff_max < backoff_min:
        raise ConfigError(
            f"{WORKER_BACKOFF_MAX_KEY} must be at least {WORKER_BACKOFF_MIN_KEY}"
            f" ({backoff_min}), got {backoff_max}"
        )
    return WorkerSettings(
        interval_seconds=interval,
        backoff_min_seconds=backoff_min,
        backoff_max_seconds=backoff_max,
        poll_safety_margin_seconds=_non_negative_float(
            config,
            POLL_SAFETY_MARGIN_KEY,
            defaults.poll_safety_margin_seconds,
        ),
        poll_alert_after_failures=_positive_int(
            config,
            POLL_ALERT_AFTER_FAILURES_KEY,
            defaults.poll_alert_after_failures,
        ),
    )


def _non_negative_float(config: Config, name: str, default: float) -> float:
    raw = config.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from error
    if not value >= 0:
        raise ConfigError(f"{name} must be non-negative, got {raw!r}")
    return value


def _positive_float(config: Config, name: str, default: float) -> float:
    raw = config.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from error
    if not value > 0:
        raise ConfigError(f"{name} must be positive, got {raw!r}")
    return value


#: O9-6 owner-approved defaults for oversize-bucket partitioning (FR-BKT-11):
#: 40 relevance messages and 20 proposal candidates per part. The values are
#: configuration, not SRS numbers; they are revisited after the Phase 10
#: benchmark. A configured limit is never a reason to cut a message.
DEFAULT_RELEVANCE_MAX_MESSAGES_PER_CALL = 40
DEFAULT_PROPOSAL_MAX_CANDIDATES_PER_CALL = 20

#: Configuration keys read by :func:`load_part_settings`.
RELEVANCE_MAX_MESSAGES_KEY = "RELEVANCE_MAX_MESSAGES_PER_CALL"
PROPOSAL_MAX_CANDIDATES_KEY = "PROPOSAL_MAX_CANDIDATES_PER_CALL"


@dataclass(frozen=True)
class PartSettings:
    """Validated per-call part sizes for the two AI stages (FR-BKT-11).

    One part size per stage: relevance partitions a bucket's messages,
    proposals partition the candidate set. Both are positive integers, passed
    explicitly into the passes so a run is reproducible from its configuration.
    """

    relevance_max_messages_per_call: int = DEFAULT_RELEVANCE_MAX_MESSAGES_PER_CALL
    proposal_max_candidates_per_call: int = DEFAULT_PROPOSAL_MAX_CANDIDATES_PER_CALL


def load_part_settings(config: Config) -> PartSettings:
    """Build :class:`PartSettings` from optional configuration overrides.

    Absent or blank values keep the O9-6 defaults. A value that is not a
    positive integer raises :class:`ConfigError` before any pass runs — an
    invalid limit is never clamped silently (NFR-MNT-01).
    """
    defaults = PartSettings()
    return PartSettings(
        relevance_max_messages_per_call=_positive_int(
            config,
            RELEVANCE_MAX_MESSAGES_KEY,
            defaults.relevance_max_messages_per_call,
        ),
        proposal_max_candidates_per_call=_positive_int(
            config,
            PROPOSAL_MAX_CANDIDATES_KEY,
            defaults.proposal_max_candidates_per_call,
        ),
    )


def _positive_int(config: Config, name: str, default: int) -> int:
    raw = config.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < 1:
        raise ConfigError(f"{name} must be positive, got {raw!r}")
    return value


def _bounded_int(
    config: Config, name: str, default: int, *, minimum: int, maximum: int
) -> int:
    value = _positive_int(config, name, default)
    if value < minimum or value > maximum:
        raise ConfigError(
            f"{name} must be between {minimum} and {maximum}, got {value}"
        )
    return value


#: O11-3 owner-approved notification delivery policy: five attempts, a 5 s
#: exponential base capped at 5 minutes, and at most 50 rows per batch. Every
#: value is configuration and validated; no call site hard-codes it.
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 5
DEFAULT_NOTIFICATION_BACKOFF_BASE_SECONDS = 5.0
DEFAULT_NOTIFICATION_BACKOFF_MAX_SECONDS = 300.0
DEFAULT_NOTIFICATION_BATCH_LIMIT = 50

NOTIFICATION_MAX_ATTEMPTS_KEY = "NOTIFICATION_MAX_ATTEMPTS"
NOTIFICATION_BACKOFF_BASE_KEY = "NOTIFICATION_BACKOFF_BASE_SECONDS"
NOTIFICATION_BACKOFF_MAX_KEY = "NOTIFICATION_BACKOFF_MAX_SECONDS"
NOTIFICATION_BATCH_LIMIT_KEY = "NOTIFICATION_BATCH_LIMIT"


@dataclass(frozen=True)
class NotificationSettings:
    """Validated notification retry and batch policy (O11-3, FR-NOT-04/05)."""

    max_attempts: int = DEFAULT_NOTIFICATION_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_NOTIFICATION_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_NOTIFICATION_BACKOFF_MAX_SECONDS
    batch_limit: int = DEFAULT_NOTIFICATION_BATCH_LIMIT


def load_notification_settings(config: Config) -> NotificationSettings:
    """Build :class:`NotificationSettings`; invalid values are rejected."""
    defaults = NotificationSettings()
    base = _positive_float(
        config, NOTIFICATION_BACKOFF_BASE_KEY, defaults.backoff_base_seconds
    )
    cap = _positive_float(
        config, NOTIFICATION_BACKOFF_MAX_KEY, defaults.backoff_max_seconds
    )
    if cap < base:
        raise ConfigError(
            f"{NOTIFICATION_BACKOFF_MAX_KEY} must be at least"
            f" {NOTIFICATION_BACKOFF_BASE_KEY} ({base}), got {cap}"
        )
    return NotificationSettings(
        max_attempts=_positive_int(
            config, NOTIFICATION_MAX_ATTEMPTS_KEY, defaults.max_attempts
        ),
        backoff_base_seconds=base,
        backoff_max_seconds=cap,
        batch_limit=_positive_int(
            config, NOTIFICATION_BATCH_LIMIT_KEY, defaults.batch_limit
        ),
    )


#: Discord REST polling (plan §4.7, O9-1). The token is read with
#: :meth:`Config.require` at the call site and never stored in these settings.
DISCORD_BOT_TOKEN_KEY = "DISCORD_BOT_TOKEN"

DEFAULT_DISCORD_PAGE_LIMIT = 100
DEFAULT_DISCORD_TIMEOUT_SECONDS = 10.0
DEFAULT_DISCORD_MAX_ATTEMPTS = 4
DEFAULT_DISCORD_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_DISCORD_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_DISCORD_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_DISCORD_MAX_PAGES = 10

DISCORD_PAGE_LIMIT_KEY = "DISCORD_PAGE_LIMIT"
DISCORD_TIMEOUT_KEY = "DISCORD_TIMEOUT_SECONDS"
DISCORD_MAX_ATTEMPTS_KEY = "DISCORD_MAX_ATTEMPTS"
DISCORD_BACKOFF_BASE_KEY = "DISCORD_BACKOFF_BASE_SECONDS"
DISCORD_BACKOFF_MAX_KEY = "DISCORD_BACKOFF_MAX_SECONDS"
DISCORD_POLL_INTERVAL_KEY = "DISCORD_POLL_INTERVAL_SECONDS"
DISCORD_MAX_PAGES_KEY = "DISCORD_MAX_PAGES"
DISCORD_ALLOWED_BOT_IDS_KEY = "DISCORD_ALLOWED_BOT_USER_IDS"


@dataclass(frozen=True)
class DiscordSettings:
    """Validated Discord REST polling limits (plan §4.7).

    Discord caps ``limit`` at 100. Every value is configuration; an invalid
    value raises :class:`ConfigError` instead of being clamped silently.
    ``allowed_bot_user_ids`` (W8b) is the explicit exception to the bot filter:
    a bot or webhook author whose id is listed is ingested like a human.
    """

    page_limit: int = DEFAULT_DISCORD_PAGE_LIMIT
    timeout_seconds: float = DEFAULT_DISCORD_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_DISCORD_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_DISCORD_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_DISCORD_BACKOFF_MAX_SECONDS
    poll_interval_seconds: float = DEFAULT_DISCORD_POLL_INTERVAL_SECONDS
    max_pages: int = DEFAULT_DISCORD_MAX_PAGES
    allowed_bot_user_ids: frozenset[str] = frozenset()


def load_discord_settings(config: Config) -> DiscordSettings:
    """Build :class:`DiscordSettings` from optional configuration overrides."""
    defaults = DiscordSettings()
    timeout = _positive_float(
        config, DISCORD_TIMEOUT_KEY, defaults.timeout_seconds
    )
    backoff_base = _positive_float(
        config, DISCORD_BACKOFF_BASE_KEY, defaults.backoff_base_seconds
    )
    backoff_max = _positive_float(
        config, DISCORD_BACKOFF_MAX_KEY, defaults.backoff_max_seconds
    )
    if backoff_max < backoff_base:
        raise ConfigError(
            f"{DISCORD_BACKOFF_MAX_KEY} must be at least {DISCORD_BACKOFF_BASE_KEY}"
            f" ({backoff_base}), got {backoff_max}"
        )
    return DiscordSettings(
        page_limit=_bounded_int(
            config,
            DISCORD_PAGE_LIMIT_KEY,
            defaults.page_limit,
            minimum=1,
            maximum=100,
        ),
        timeout_seconds=timeout,
        max_attempts=_positive_int(
            config, DISCORD_MAX_ATTEMPTS_KEY, defaults.max_attempts
        ),
        backoff_base_seconds=backoff_base,
        backoff_max_seconds=backoff_max,
        poll_interval_seconds=_positive_float(
            config, DISCORD_POLL_INTERVAL_KEY, defaults.poll_interval_seconds
        ),
        max_pages=_positive_int(
            config, DISCORD_MAX_PAGES_KEY, defaults.max_pages
        ),
        allowed_bot_user_ids=_discord_id_set(
            config,
            DISCORD_ALLOWED_BOT_IDS_KEY,
            defaults.allowed_bot_user_ids,
        ),
    )


def _discord_id_set(
    config: Config, name: str, default: frozenset[str]
) -> frozenset[str]:
    """Parse a comma-separated Discord-id allow list (W8b).

    Blank means the default (empty). Every entry must be a non-empty numeric
    Discord id; an invalid entry is a configuration error, never silently
    dropped.
    """
    raw = config.get(name)
    if raw is None or raw.strip() == "":
        return default
    values: set[str] = set()
    for part in raw.split(","):
        item = part.strip()
        if item == "" or not item.isascii() or not item.isdigit():
            raise ConfigError(
                f"{name} must be a comma-separated list of numeric Discord ids,"
                f" got {raw!r}"
            )
        values.add(item)
    return frozenset(values)


#: W9 H4: SQLite busy timeout used when a connection is opened. The default is
#: the previous fixed 5000 ms; it is now configuration and validated.
SQLITE_BUSY_TIMEOUT_KEY = "SQLITE_BUSY_TIMEOUT_MS"
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class SqliteSettings:
    """Validated SQLite connection settings."""

    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS


def load_sqlite_settings(config: Config) -> SqliteSettings:
    """Build :class:`SqliteSettings`; a non-positive timeout is rejected."""
    return SqliteSettings(
        busy_timeout_ms=_positive_int(
            config, SQLITE_BUSY_TIMEOUT_KEY, DEFAULT_SQLITE_BUSY_TIMEOUT_MS
        )
    )


#: W9 H1: the ``asec`` logging namespace is configured from the environment.
#: ``LOG_LEVEL`` defaults to INFO and is validated; ``LOG_FILE`` is optional and
#: turns on the rotating file handler. Neither key holds a secret.
LOG_LEVEL_KEY = "LOG_LEVEL"
LOG_FILE_KEY = "LOG_FILE"
DEFAULT_LOG_LEVEL = "INFO"
_VALID_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


@dataclass(frozen=True)
class LoggingSettings:
    """Validated logging configuration for the ``asec`` logger namespace."""

    level: str = DEFAULT_LOG_LEVEL
    log_file: str | None = None


def load_logging_settings(config: Config) -> LoggingSettings:
    """Build :class:`LoggingSettings`; an unknown level is a configuration error."""
    raw_level = config.get(LOG_LEVEL_KEY)
    level = DEFAULT_LOG_LEVEL if raw_level is None else raw_level.strip().upper()
    if level == "":
        level = DEFAULT_LOG_LEVEL
    if level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            f"{LOG_LEVEL_KEY} must be one of"
            f" {', '.join(sorted(_VALID_LOG_LEVELS))}, got {raw_level!r}"
        )
    raw_file = config.get(LOG_FILE_KEY)
    log_file = None if raw_file is None or raw_file.strip() == "" else raw_file.strip()
    return LoggingSettings(level=level, log_file=log_file)


#: W9 H6 (SRS DR-07): retention runs from the festival end date the Admin
#: records. This is configuration only; no purge code exists yet.
FESTIVAL_END_DATE_KEY = "FESTIVAL_END_DATE"
RETENTION_MONTHS_KEY = "RETENTION_MONTHS"
DEFAULT_RETENTION_MONTHS = 3


@dataclass(frozen=True)
class RetentionSettings:
    """Validated retention inputs: unset festival end and calendar months."""

    festival_end_date: date | None = None
    retention_months: int = DEFAULT_RETENTION_MONTHS


def load_retention_settings(config: Config) -> RetentionSettings:
    """Build :class:`RetentionSettings`; invalid dates or months are rejected."""
    raw_date = config.get(FESTIVAL_END_DATE_KEY)
    festival_end: date | None = None
    if raw_date is not None and raw_date.strip() != "":
        try:
            festival_end = date.fromisoformat(raw_date.strip())
        except ValueError as error:
            raise ConfigError(
                f"{FESTIVAL_END_DATE_KEY} must be an ISO date (YYYY-MM-DD),"
                f" got {raw_date!r}"
            ) from error
    raw_months = config.get(RETENTION_MONTHS_KEY)
    months = DEFAULT_RETENTION_MONTHS
    if raw_months is not None and raw_months.strip() != "":
        try:
            months = int(raw_months.strip())
        except ValueError as error:
            raise ConfigError(
                f"{RETENTION_MONTHS_KEY} must be a positive integer,"
                f" got {raw_months!r}"
            ) from error
        if months < 1:
            raise ConfigError(
                f"{RETENTION_MONTHS_KEY} must be a positive integer,"
                f" got {raw_months!r}"
            )
    return RetentionSettings(festival_end_date=festival_end, retention_months=months)


def retention_date(festival_end: date | None, months: int) -> date:
    """Return the retention date ``months`` calendar months after ``festival_end``.

    A day that does not exist in the target month is clamped to that month's
    last day (for example 30 November + 3 months = 28 February, or 29 February
    in a leap year). The function is pure: it deletes nothing and reads no clock.
    """
    if festival_end is None:
        raise ValueError("festival_end is required to compute a retention date")
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError(f"months must be a positive integer, got {months!r}")
    month_index = festival_end.month - 1 + months
    year = festival_end.year + month_index // 12
    month = month_index % 12 + 1
    day = min(festival_end.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def retention_due(
    today: date, festival_end: date | None, months: int
) -> bool:
    """Return True once ``today`` is at or after the retention date.

    An unset festival end means retention has not started. This is a pure
    predicate; the purge procedure itself is not part of the MVP.
    """
    if festival_end is None:
        return False
    return today >= retention_date(festival_end, months)
