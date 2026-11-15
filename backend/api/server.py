"""API app factory, error handlers and the server runner (W2; plan §4.2).

There is one app factory and no module-level app: ``create_app`` takes the
connection factory, the clock and the configuration, validates the
configuration, and builds everything else. Nothing starts a worker, opens a
connection or runs a migration at import or startup time.

The framework and uvicorn are imported here and nowhere else in ``backend``,
and only behind the optional ``api`` extra. A missing extra raises
:class:`backend.api.ApiConfigurationError`, never a bare import error.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.api import ApiConfigurationError
from backend.api.errors import (
    MAX_BODY_BYTES,
    ApiError,
    code_for_status,
    error_body,
    status_for,
)
from backend.config import (
    Config,
    load_api_read_settings,
    load_auth_settings,
    load_idempotency_settings,
)
from backend.notification_delivery import OtpSender, OtpSenderNotConfigured

#: The optional extra install command; it is a command, not a secret.
API_EXTRA_COMMAND = 'python -m pip install -e ".[dev,api]"'

try:  # the optional extra; a clear configuration error when it is absent
    from fastapi import FastAPI, Request, Response
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ImportError as error:  # pragma: no cover - exercised by a blocked import
    raise ApiConfigurationError(
        "the ASEC API requires the optional 'api' extra; install it with"
        f" `{API_EXTRA_COMMAND}`"
    ) from error

# Only importable once the guarded block above proved the framework is present,
# so it cannot be hoisted above it.
from backend.api.handlers import router  # noqa: E402

logger = logging.getLogger("asec.api")


class _UnconfiguredOtpSender:
    """Stands in when no sender is configured; records SENDER_UNCONFIGURED.

    Sign-in must not fail and must not reveal that nothing was sent, so the
    request path behaves exactly like a failed DM (Phase 11 contract).
    """

    def send(self, *, discord_id: str, code: str) -> None:
        raise OtpSenderNotConfigured("OTP sender is not configured")

    def __repr__(self) -> str:
        return "UnconfiguredOtpSender()"


def create_app(
    *,
    connection_factory: Callable[[], sqlite3.Connection],
    now: Callable[[], str],
    config: Config,
    otp_sender: OtpSender | None = None,
) -> FastAPI:
    """Build the API app. Invalid configuration fails here, before any bind."""
    auth_settings = load_auth_settings(config)
    idempotency_settings = load_idempotency_settings(config)
    read_settings = load_api_read_settings(config)
    app = FastAPI(
        title="ASEC API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.connection_factory = connection_factory
    app.state.now = now
    app.state.config = config
    app.state.auth_settings = auth_settings
    app.state.idempotency_settings = idempotency_settings
    app.state.read_settings = read_settings
    app.state.otp_sender = (
        _UnconfiguredOtpSender() if otp_sender is None else otp_sender
    )

    _register_error_handlers(app)
    _register_request_guard(app)
    app.include_router(router)
    return app


def run_server(app: FastAPI, *, host: str, port: int) -> None:
    """Run the app with uvicorn; the only uvicorn import in the backend."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


def _register_request_guard(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_and_body_cap(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = secrets.token_hex(8)
        request.state.request_id = request_id
        started = time.monotonic()
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > MAX_BODY_BYTES
            except ValueError:
                too_large = True
            if too_large:
                rejected: Response = _error_response(request, "INVALID_REQUEST")
                _log_request(request, rejected.status_code, request_id, started)
                return rejected
        # Cache the raw body once: the idempotency digest is over the exact
        # bytes the client sent, and the framework reads the same cached copy.
        request.state.raw_body = await request.body()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        _log_request(request, response.status_code, request_id, started)
        return response


def _log_request(
    request: Request, status: int, request_id: str, started: float
) -> None:
    """One structured line per request: metadata only, never a value."""
    # ``url.path`` excludes the query string, so a token can never reach the log
    # even if a client wrongly puts one there.
    logger.info(
        "api request method=%s path=%s status=%d request_id=%s actor=%s"
        " duration_ms=%.1f",
        request.method,
        request.url.path,
        status,
        request_id,
        getattr(request.state, "person_id", "-"),
        (time.monotonic() - started) * 1000.0,
    )


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(request, exc.code, exc.payload)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # The framework's detail can echo the rejected input; it is never sent.
        return _error_response(request, "INVALID_REQUEST")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _error_response(request, code_for_status(exc.status_code))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Type only: no message, SQL, path, token or code reaches a log line.
        logger.error(
            "api request failed request_id=%s error_type=%s",
            _request_id(request),
            type(exc).__name__,
        )
        return _error_response(request, "INTERNAL_ERROR")


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return "-" if value is None else str(value)


def _error_response(
    request: Request,
    code: str,
    payload: dict[str, Any] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_for(code),
        content=error_body(code, request_id, payload),
        headers={"X-Request-ID": request_id},
    )
