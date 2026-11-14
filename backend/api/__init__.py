"""ASEC HTTP API adapter (Phase 12 W2; O12-1).

The framework and its server are an optional ``api`` extra, confined to this
package. Importing ``backend.api`` never imports them: :func:`create_app`
imports :mod:`backend.api.server` on demand, and a missing extra raises
:class:`ApiConfigurationError` there instead of a bare ``ModuleNotFoundError``.
No domain module imports this package or the framework.

The API is a thin adapter: parse, authenticate, call one domain service, map
errors, serialize. Authorization stays inside the services (FR-AUT-01).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from backend.config import Config
from backend.notification_delivery import OtpSender

if TYPE_CHECKING:  # the framework is never imported at runtime by this package
    from fastapi import FastAPI

#: Shown when the optional extra is absent. It is a command, not a secret.
API_EXTRA_COMMAND = 'python -m pip install -e ".[dev,api]"'


class ApiConfigurationError(RuntimeError):
    """The optional ``api`` extra is missing or the API configuration is invalid."""


def create_app(
    *,
    connection_factory: Callable[[], sqlite3.Connection],
    now: Callable[[], str],
    config: Config,
    otp_sender: OtpSender | None = None,
) -> FastAPI:
    """Build one API app; see :func:`backend.api.server.create_app`."""
    from backend.api.server import create_app as _create_app

    return _create_app(
        connection_factory=connection_factory,
        now=now,
        config=config,
        otp_sender=otp_sender,
    )
