"""Uniform API errors (W2; plan §4.2.5, EIR-02, NFR-SEC-01).

One JSON shape for every failure, a fixed status/code table, and a
server-generated request id. A message is a constant per code: no field name,
input value, SQL, file path, token or OTP code is ever echoed, and a 500 says
nothing beyond "the server could not complete the request".
"""

from __future__ import annotations

ERROR_MESSAGES: dict[str, tuple[int, str]] = {
    "UNAUTHENTICATED": (401, "Authentication is required."),
    "FORBIDDEN": (403, "The operation is not permitted."),
    "NOT_FOUND": (404, "The resource was not found."),
    "CONFLICT": (409, "The request conflicts with the current state."),
    "INVALID_REQUEST": (422, "The request is not valid."),
    "RATE_LIMITED": (429, "Too many requests; try again later."),
    "INTERNAL_ERROR": (500, "The server could not complete the request."),
}

#: A declared request body larger than this is refused before parsing.
MAX_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    """A mapped API failure. The code must belong to the fixed table."""

    def __init__(self, code: str) -> None:
        if code not in ERROR_MESSAGES:
            raise ValueError(f"unknown API error code {code!r}")
        self.code = code
        self.status = ERROR_MESSAGES[code][0]
        super().__init__(code)


def status_for(code: str) -> int:
    """Return the HTTP status for a code in the table."""
    return ERROR_MESSAGES[code][0]


def code_for_status(status: int) -> str:
    """Map a framework status onto the fixed table (server errors stay 500)."""
    for code, (mapped, _message) in ERROR_MESSAGES.items():
        if mapped == status:
            return code
    return "INTERNAL_ERROR" if status >= 500 else "INVALID_REQUEST"


def error_body(code: str, request_id: str) -> dict[str, object]:
    """The one error shape; ``request_id`` is generated server-side."""
    _status, message = ERROR_MESSAGES[code]
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
        }
    }
