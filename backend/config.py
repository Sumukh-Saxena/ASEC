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
