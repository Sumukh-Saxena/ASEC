"""Environment-based configuration for ASEC (Phase 0 foundation).

Values are read from the process environment, not from committed files. Diagnostic
output (``repr``, ``str`` and :meth:`Config.as_public_dict`) exposes configuration
key names only and never a configured value, so a config object is safe to log.
Actual values are available only through :meth:`Config.get` and
:meth:`Config.require`. This is a foundation only; it does not load ``.env`` or
implement any domain configuration.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

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


@dataclass(frozen=True)
class WorkerSettings:
    """Validated worker-loop timing (O9-3, NFR-MNT-01).

    Defaults are the owner-approved values: a 2 s interval with a 1-60 s
    exponential backoff after a failed tick. Every value is configuration,
    never a hard-coded constant at the call site.
    """

    interval_seconds: float = 2.0
    backoff_min_seconds: float = 1.0
    backoff_max_seconds: float = 60.0


#: Configuration keys read by :func:`load_worker_settings`.
WORKER_INTERVAL_KEY = "WORKER_INTERVAL_SECONDS"
WORKER_BACKOFF_MIN_KEY = "WORKER_BACKOFF_MIN_SECONDS"
WORKER_BACKOFF_MAX_KEY = "WORKER_BACKOFF_MAX_SECONDS"


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
    )


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


@dataclass(frozen=True)
class DiscordSettings:
    """Validated Discord REST polling limits (plan §4.7).

    Discord caps ``limit`` at 100. Every value is configuration; an invalid
    value raises :class:`ConfigError` instead of being clamped silently.
    """

    page_limit: int = DEFAULT_DISCORD_PAGE_LIMIT
    timeout_seconds: float = DEFAULT_DISCORD_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_DISCORD_MAX_ATTEMPTS
    backoff_base_seconds: float = DEFAULT_DISCORD_BACKOFF_BASE_SECONDS
    backoff_max_seconds: float = DEFAULT_DISCORD_BACKOFF_MAX_SECONDS
    poll_interval_seconds: float = DEFAULT_DISCORD_POLL_INTERVAL_SECONDS
    max_pages: int = DEFAULT_DISCORD_MAX_PAGES


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
    )
