"""Stdlib JSON HTTP adapter for the AI protocols (Phase 10, W1).

Owner-approved wire contract (O10-1, plan §4.1): one POST per call carrying
``function``, ``contract_version``, ``model_id``, ``prompt_version`` and the
model-facing ``input``; the response carries provenance and exactly the array
for that function. No dependency beyond ``urllib.request`` and ``json``.

The adapter is a transport only: it does not validate against the bucket, does
not retry at pass level, does not write, and never trusts the response. The
existing passes own validation, attempt recording and fallback order.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol, overload

from backend.masking import mask_pii
from backend.proposals import (
    ProposalDraft,
    ProposalGenerationRequest,
    ProposalResponse,
)
from backend.relevance import (
    MessageDecision,
    RelevanceRequest,
    RelevanceResponse,
)

CONTRACT_VERSION = "1"
DEFAULT_RELEVANCE_PATH = "/v1/relevance"
DEFAULT_PROPOSAL_PATH = "/v1/proposals"

_RETRY_BASE_SECONDS = 0.5
_RETRY_MAX_SECONDS = 8.0

_RELEVANCE_RESPONSE_KEYS = frozenset(
    {"model_id", "model_version", "prompt_version", "decisions"}
)
_DECISION_KEYS = frozenset({"message_id", "decision"})
_PROPOSAL_RESPONSE_KEYS = frozenset(
    {"model_id", "model_version", "prompt_version", "proposals"}
)
_PROPOSAL_ITEM_KEYS = frozenset(
    {
        "primary_message_id",
        "ptype",
        "description",
        "assignment_mode",
        "proposed_assignees",
        "people_needed",
        "people_needed_unlimited",
        "task_type",
        "suggested_responsible_vertical",
        "affected_verticals",
        "attributes",
    }
)

_MENTION_HANDLE_RE = re.compile(r"@m\d+\Z")
_GROUP_HANDLE_RE = re.compile(r"g\d+\Z")
_RAW_IDENTIFIER_PATTERNS = (
    re.compile(r"<@!?\d+>"),
    re.compile(r"<@&\d+>"),
    re.compile(r"<#\d+>"),
    re.compile(r":[A-Za-z_][A-Za-z0-9_]*:\d+"),
)
_FORBIDDEN_FIELD_NAMES = frozenset(
    {"sender_discord_user_id", "discord_channel_id", "raw_metadata"}
)
_RELEVANCE_FIELDS = frozenset(
    {"bucket_id", "group_handle", "vertical_name", "instructions", "messages"}
)
_RELEVANCE_MESSAGE_FIELDS = frozenset(
    {
        "message_id",
        "sender_person_id",
        "sender_name",
        "event_timestamp",
        "reply_to_message_id",
        "masked_content",
        "mention_handles",
    }
)
_PROPOSAL_FIELDS = frozenset(
    {
        "bucket_id",
        "vertical_name",
        "candidate_messages",
        "context_messages",
        "instructions",
    }
)
_PROPOSAL_MESSAGE_FIELDS = frozenset(
    {
        "message_id",
        "sender_name",
        "event_timestamp",
        "reply_reference",
        "content",
        "mention_handles",
    }
)


class AiAdapterError(Exception):
    """Base class for adapter-side failures."""


class UnsafeRequestError(AiAdapterError):
    """The request would leak an identifier or personal data to the model."""


class AdapterContractError(AiAdapterError):
    """The model output is malformed or outside the agreed wire contract."""


class ProvenanceMismatchError(AdapterContractError):
    """The response provenance does not match the configured primary model."""


class HttpStatusError(AiAdapterError):
    """A non-retryable (or exhausted) HTTP status response."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class TransportFailure(AiAdapterError):
    """A transport-level failure (timeout, connection error) after retries."""


class HttpResponse:
    """The transport result; a 4xx/5xx is returned, not raised."""

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = status
        self.headers = dict(headers)
        self.body = body


class Transport(Protocol):
    """The only network seam; tests inject a fake implementation."""

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Default stdlib transport (no dependency, no network in tests)."""

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=int(error.code),
                headers=dict(error.headers or {}),
                body=error.read(),
            )


def _assert_no_raw_identifier(value: str, label: str) -> None:
    for pattern in _RAW_IDENTIFIER_PATTERNS:
        if pattern.search(value):
            raise UnsafeRequestError(
                f"{label} still contains a raw Discord identifier"
            )


def _mask_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    masked = mask_pii(value)
    _assert_no_raw_identifier(masked, label)
    return masked


def _mask_required_text(value: str, label: str) -> str:
    masked = _mask_text(value, label)
    if masked is None:
        raise UnsafeRequestError(f"{label} must not be null")
    return masked


def _assert_handle(value: object, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise UnsafeRequestError(f"{label} is not an opaque handle")


def _assert_only_fields(
    request: object, allowed: frozenset[str], label: str
) -> None:
    present = set(getattr(request, "__dict__", {}))
    if present - allowed:
        # Never echo the field name: a subclass may have put an identifier or
        # content there. The refusal alone is enough.
        raise UnsafeRequestError(
            f"{label} carries fields outside the EIR-03 request contract"
        )
    if present & _FORBIDDEN_FIELD_NAMES:
        raise UnsafeRequestError(
            f"{label} carries backend-only fields that must not leave the backend"
        )


def _safe_relevance_message(message: Any) -> Any:
    _assert_only_fields(message, _RELEVANCE_MESSAGE_FIELDS, "request message")
    for handle in message.mention_handles:
        _assert_handle(handle, _MENTION_HANDLE_RE, "mention_handle")
    return replace(
        message,
        sender_name=_mask_text(message.sender_name, "sender_name"),
        event_timestamp=_mask_text(message.event_timestamp, "event_timestamp"),
        masked_content=_mask_text(message.masked_content, "masked_content"),
    )


def _safe_proposal_message(message: Any) -> Any:
    _assert_only_fields(message, _PROPOSAL_MESSAGE_FIELDS, "request message")
    for handle in message.mention_handles:
        _assert_handle(handle, _MENTION_HANDLE_RE, "mention_handle")
    return replace(
        message,
        sender_name=_mask_text(message.sender_name, "sender_name"),
        event_timestamp=_mask_text(message.event_timestamp, "event_timestamp"),
        content=_mask_text(message.content, "content"),
    )


def _safe_relevance_request(request: RelevanceRequest) -> RelevanceRequest:
    _assert_only_fields(request, _RELEVANCE_FIELDS, "request")
    _assert_handle(request.group_handle, _GROUP_HANDLE_RE, "group_handle")
    return replace(
        request,
        vertical_name=_mask_text(request.vertical_name, "vertical_name"),
        instructions=_mask_required_text(request.instructions, "instructions"),
        messages=tuple(_safe_relevance_message(m) for m in request.messages),
    )


def _safe_proposal_request(
    request: ProposalGenerationRequest,
) -> ProposalGenerationRequest:
    _assert_only_fields(request, _PROPOSAL_FIELDS, "request")
    return replace(
        request,
        vertical_name=_mask_text(request.vertical_name, "vertical_name"),
        instructions=_mask_required_text(request.instructions, "instructions"),
        candidate_messages=tuple(
            _safe_proposal_message(m) for m in request.candidate_messages
        ),
        context_messages=tuple(
            _safe_proposal_message(m) for m in request.context_messages
        ),
    )


@overload
def assert_safe_request(request: RelevanceRequest) -> RelevanceRequest: ...


@overload
def assert_safe_request(
    request: ProposalGenerationRequest,
) -> ProposalGenerationRequest: ...


def assert_safe_request(
    request: RelevanceRequest | ProposalGenerationRequest,
) -> RelevanceRequest | ProposalGenerationRequest:
    """Return a re-masked, identifier-free copy of the request (plan §4.3).

    Refuses before any serialization or dispatch. The original request (and the
    stored source content behind it) is never mutated (DR-03, AC-21).
    """
    if isinstance(request, RelevanceRequest):
        return _safe_relevance_request(request)
    if isinstance(request, ProposalGenerationRequest):
        return _safe_proposal_request(request)
    raise UnsafeRequestError("unsupported request type")


def _relevance_input(request: RelevanceRequest) -> dict[str, object]:
    """The model-facing relevance input (handles only, no Discord ids)."""
    return {
        "bucket_id": request.bucket_id,
        "group_handle": request.group_handle,
        "vertical_name": request.vertical_name,
        "instructions": request.instructions,
        "messages": [
            {
                "message_id": message.message_id,
                "sender_person_id": message.sender_person_id,
                "sender_name": message.sender_name,
                "event_timestamp": message.event_timestamp,
                "reply_to_message_id": message.reply_to_message_id,
                "masked_content": message.masked_content,
                "mention_handles": list(message.mention_handles),
            }
            for message in request.messages
        ],
    }


def _proposal_input(request: ProposalGenerationRequest) -> dict[str, object]:
    """The model-facing proposal input (no handle map, handles only)."""
    return {
        "bucket_id": request.bucket_id,
        "vertical_name": request.vertical_name,
        "instructions": request.instructions,
        "candidate_messages": [
            _proposal_message(message) for message in request.candidate_messages
        ],
        "context_messages": [
            _proposal_message(message) for message in request.context_messages
        ],
    }


def _proposal_message(message: Any) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "sender_name": message.sender_name,
        "event_timestamp": message.event_timestamp,
        "reply_reference": message.reply_reference,
        "content": message.content,
        "mention_handles": list(message.mention_handles),
    }


def _require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterContractError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], allowed: frozenset[str], label: str
) -> None:
    keys = set(value)
    if keys - allowed:
        raise AdapterContractError(f"{label} carries unknown keys")
    missing = allowed - keys
    if missing:
        raise AdapterContractError(f"{label} is missing required keys")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise AdapterContractError(f"{label} must be a non-blank string")
    return value


def _parse_relevance(data: object) -> RelevanceResponse:
    response = _require_object(data, "relevance response")
    _require_exact_keys(
        response, _RELEVANCE_RESPONSE_KEYS, "relevance response"
    )
    decisions = response["decisions"]
    if not isinstance(decisions, list):
        raise AdapterContractError("relevance response must carry a decisions array")
    parsed: list[MessageDecision] = []
    for item in decisions:
        decision_object = _require_object(item, "decision")
        _require_exact_keys(decision_object, _DECISION_KEYS, "decision")
        message_id = decision_object["message_id"]
        decision = decision_object["decision"]
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise AdapterContractError("decision message_id must be an integer")
        if not isinstance(decision, str):
            raise AdapterContractError("decision must be a string")
        parsed.append(MessageDecision(message_id=message_id, decision=decision))
    return RelevanceResponse(
        model_id=_required_text(response["model_id"], "model_id"),
        model_version=_required_text(
            response["model_version"], "model_version"
        ),
        prompt_version=_required_text(
            response["prompt_version"], "prompt_version"
        ),
        decisions=tuple(parsed),
    )


def _tuple_of_text(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise AdapterContractError(f"{label} must be a JSON array or null")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AdapterContractError(f"{label} items must be strings")
        items.append(item)
    return tuple(items)


def _attributes(value: object) -> tuple[tuple[str, str | None], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise AdapterContractError("attributes must be a JSON array")
    parsed: list[tuple[str, str | None]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise AdapterContractError("an attribute must be a two-item array")
        key, raw_value = item
        if not isinstance(key, str):
            raise AdapterContractError("an attribute key must be a string")
        if raw_value is not None and not isinstance(raw_value, str):
            raise AdapterContractError("an attribute value must be a string or null")
        parsed.append((key, raw_value))
    return tuple(parsed)


def _parse_proposals(data: object) -> ProposalResponse:
    response = _require_object(data, "proposal response")
    _require_exact_keys(response, _PROPOSAL_RESPONSE_KEYS, "proposal response")
    proposals = response["proposals"]
    if not isinstance(proposals, list):
        raise AdapterContractError("proposal response must carry a proposals array")
    parsed: list[ProposalDraft] = []
    for item in proposals:
        proposal = _require_object(item, "proposal")
        _require_exact_keys(proposal, _PROPOSAL_ITEM_KEYS, "proposal")
        primary = proposal["primary_message_id"]
        ptype = proposal["ptype"]
        if isinstance(primary, bool) or not isinstance(primary, int):
            raise AdapterContractError("proposal primary_message_id must be an integer")
        if not isinstance(ptype, str):
            raise AdapterContractError("proposal ptype must be a string")
        parsed.append(
            ProposalDraft(
                primary_message_id=primary,
                ptype=ptype,
                description=proposal.get("description"),
                assignment_mode=proposal.get("assignment_mode", "EXPLICIT"),
                proposed_assignees=_tuple_of_text(
                    proposal.get("proposed_assignees"), "proposed_assignees"
                ),
                people_needed=proposal.get("people_needed"),
                people_needed_unlimited=proposal.get(
                    "people_needed_unlimited", 0
                ),
                task_type=proposal.get("task_type", "INTERNAL"),
                suggested_responsible_vertical=proposal.get(
                    "suggested_responsible_vertical"
                ),
                affected_verticals=(
                    _tuple_of_text(
                        proposal.get("affected_verticals"), "affected_verticals"
                    )
                    or ()
                ),
                attributes=_attributes(proposal.get("attributes")),
            )
        )
    return ProposalResponse(
        model_id=_required_text(response["model_id"], "model_id"),
        model_version=_required_text(
            response["model_version"], "model_version"
        ),
        prompt_version=_required_text(
            response["prompt_version"], "prompt_version"
        ),
        proposals=tuple(parsed),
    )


class HttpModelAdapter:
    """Stateless JSON-over-HTTP adapter behind the AI protocols (EIR-03)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model_id: str,
        prompt_version: str,
        timeout_seconds: float,
        max_attempts: int,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        relevance_path: str = DEFAULT_RELEVANCE_PATH,
        proposal_path: str = DEFAULT_PROPOSAL_PATH,
    ) -> None:
        if base_url.strip() == "":
            raise ValueError("base_url must be non-empty")
        if model_id.strip() == "":
            raise ValueError("model_id must be non-empty")
        if prompt_version.strip() == "":
            raise ValueError("prompt_version must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key if api_key else None
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._transport = UrllibTransport() if transport is None else transport
        self._sleep = sleep
        self._relevance_path = relevance_path
        self._proposal_path = proposal_path

    def __repr__(self) -> str:
        # The key is deliberately absent: an adapter object is safe to log.
        return f"HttpModelAdapter(model_id={self._model_id!r})"

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        safe = assert_safe_request(request)
        data = self._post(
            "relevance", self._relevance_path, _relevance_input(safe)
        )
        response = _parse_relevance(data)
        self._check_provenance(response.model_id, "relevance")
        return response

    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
        safe = assert_safe_request(request)
        data = self._post(
            "proposal", self._proposal_path, _proposal_input(safe)
        )
        response = _parse_proposals(data)
        self._check_provenance(response.model_id, "proposal")
        return response

    def _check_provenance(self, observed_model_id: str, function: str) -> None:
        if observed_model_id != self._model_id:
            raise ProvenanceMismatchError(
                f"{function} response model {observed_model_id!r} does not match"
                f" configured model {self._model_id!r}"
            )

    def _retry_delay(
        self, attempt: int, headers: Mapping[str, str] | None = None
    ) -> float:
        if headers is not None:
            raw = headers.get("Retry-After")
            if raw is not None:
                try:
                    requested = float(raw)
                except ValueError:
                    requested = None
                if requested is not None:
                    return min(_RETRY_MAX_SECONDS, max(0.0, requested))
        return min(
            _RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        )

    def _post(
        self, function: str, path: str, input_payload: Mapping[str, object]
    ) -> object:
        payload = {
            "function": function,
            "contract_version": CONTRACT_VERSION,
            "model_id": self._model_id,
            "prompt_version": self._prompt_version,
            "input": input_payload,
        }
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}{path}"
        last_failure: AiAdapterError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._transport.post(
                    url=url, headers=headers, body=body, timeout=self._timeout
                )
            except OSError:
                last_failure = TransportFailure(
                    f"{function} transport failure after {attempt} attempt(s)"
                )
                if attempt >= self._max_attempts:
                    raise last_failure from None
                self._sleep(self._retry_delay(attempt))
                continue
            if 200 <= response.status < 300:
                return self._decode(response.body, function)
            if response.status == 429 or 500 <= response.status < 600:
                last_failure = HttpStatusError(
                    response.status,
                    f"{function} request failed with HTTP {response.status}"
                    f" after {attempt} attempt(s)",
                )
                if attempt >= self._max_attempts:
                    raise last_failure
                retry_headers = (
                    response.headers if response.status == 429 else None
                )
                self._sleep(self._retry_delay(attempt, retry_headers))
                continue
            # Any other 4xx is an immediate contract/request failure.
            raise HttpStatusError(
                response.status,
                f"{function} request failed with HTTP {response.status}",
            )
        raise last_failure or TransportFailure(
            f"{function} transport failure after {self._max_attempts} attempt(s)"
        )

    @staticmethod
    def _decode(body: bytes, function: str) -> object:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterContractError(
                f"{function} response body is not valid JSON"
            ) from error
