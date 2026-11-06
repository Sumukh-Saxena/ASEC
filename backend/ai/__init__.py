"""AI transport package (Phase 10).

The adapter is a stateless request/response transport behind the Phase 7/9
protocols. It never writes to the database and never interprets model output as
authority (EIR-03, AI-01).
"""

from __future__ import annotations

from backend.ai.http_client import (
    AdapterContractError,
    AiAdapterError,
    HttpModelAdapter,
    HttpResponse,
    HttpStatusError,
    ProvenanceMismatchError,
    Transport,
    TransportFailure,
    UnsafeRequestError,
    assert_safe_request,
)

__all__ = [
    "AdapterContractError",
    "AiAdapterError",
    "HttpModelAdapter",
    "HttpResponse",
    "HttpStatusError",
    "ProvenanceMismatchError",
    "Transport",
    "TransportFailure",
    "UnsafeRequestError",
    "assert_safe_request",
]
