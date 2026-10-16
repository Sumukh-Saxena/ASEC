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
