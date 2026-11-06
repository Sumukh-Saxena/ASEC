"""W2 tests: request guard before any AI dispatch (plan §4.3, NFR-SEC-04/AC-21).

The guard is defence in depth on top of the Phase 7 request copy: it re-masks,
refuses raw Discord identifiers and non-opaque handles, and rejects fields that
must never leave the backend. Stored content is never mutated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from backend.ai.http_client import (
    HttpModelAdapter,
    HttpResponse,
    UnsafeRequestError,
    assert_safe_request,
)
from backend.proposals import ProposalGenerationRequest, ProposalMessage
from backend.relevance import ContextMessage, RelevanceRequest

TS = "2026-01-01T10:00:00+00:00"


class NoRequestTransport:
    """A transport that fails the test if it is ever called."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {"url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        raise AssertionError("the unsafe request must not reach the transport")


def _adapter(transport: NoRequestTransport) -> HttpModelAdapter:
    return HttpModelAdapter(
        base_url="http://model.test",
        api_key=None,
        model_id="small-4b",
        prompt_version="rel-prompt-1",
        timeout_seconds=5.0,
        max_attempts=1,
        transport=transport,
    )


def _relevance_request(
    *,
    content: str = "kuch karo yaar",
    mention_handles: tuple[str, ...] = ("@m1",),
    group_handle: str = "g1",
) -> RelevanceRequest:
    return RelevanceRequest(
        bucket_id=3,
        group_handle=group_handle,
        vertical_name="Tech",
        instructions="CLASSIFICATION_POLICY: doubt policy",
        messages=(
            ContextMessage(
                message_id=11,
                sender_person_id=7,
                sender_name="Neha",
                event_timestamp=TS,
                reply_to_message_id=None,
                masked_content=content,
                mention_handles=mention_handles,
            ),
        ),
    )


def _proposal_request(*, content: str = "need volunteers @m1") -> ProposalGenerationRequest:
    message = ProposalMessage(
        message_id=11,
        sender_name="Neha",
        event_timestamp=TS,
        reply_reference=None,
        content=content,
        mention_handles=("@m1",),
    )
    return ProposalGenerationRequest(
        bucket_id=3,
        vertical_name="Tech",
        candidate_messages=(message,),
        context_messages=(message,),
        instructions="PROPOSAL_POLICY: draft only",
    )


# --- accepted opaque requests --------------------------------------------


def test_guard_accepts_opaque_handles_only() -> None:
    request = _relevance_request(content="hi @m1, please help")
    assert assert_safe_request(request) == request


# --- raw Discord identifiers rejected ------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "<@123456789012345678>",
        "<@!123456789012345678>",
        "<@&123456789012345678>",
        "<#123456789012345678>",
        "<:custom:123456789012345678>",
    ],
)
def test_guard_rejects_raw_discord_markup(raw: str) -> None:
    request = _relevance_request(content=f"hello {raw}")
    with pytest.raises(UnsafeRequestError):
        assert_safe_request(request)


def test_guard_rejects_bare_snowflake_in_mention_handle() -> None:
    request = _relevance_request()
    message = replace(
        request.messages[0],
        mention_handles=("123456789012345678",),
    )
    with pytest.raises(UnsafeRequestError):
        assert_safe_request(replace(request, messages=(message,)))


def test_guard_rejects_non_opaque_mention_handle() -> None:
    with pytest.raises(UnsafeRequestError):
        assert_safe_request(_relevance_request(mention_handles=("@neha",)))


def test_guard_rejects_non_opaque_group_handle() -> None:
    with pytest.raises(UnsafeRequestError):
        assert_safe_request(_relevance_request(group_handle="channel-1"))


# --- second masking pass -------------------------------------------------


def test_guard_masks_pii_again_without_mutating_the_request() -> None:
    original = "call 98765 43210 or mail a.b@site.co"
    request = _relevance_request(content=original)

    safe = assert_safe_request(request)

    assert safe.messages[0].masked_content == "call [PHONE] or mail [EMAIL]"
    assert request.messages[0].masked_content == original


def test_guard_masks_proposal_content_without_mutating_it() -> None:
    original = "call 98765 43210 for the stall"
    request = _proposal_request(content=original)

    safe = assert_safe_request(request)

    assert safe.candidate_messages[0].content == "call [PHONE] for the stall"
    assert request.candidate_messages[0].content == original


# --- extra / forbidden fields --------------------------------------------


@dataclass(frozen=True)
class _ExtraRelevanceRequest(RelevanceRequest):
    extra_field: str = "should-not-leave"


@dataclass(frozen=True)
class _LeakyRelevanceRequest(RelevanceRequest):
    sender_discord_user_id: str = "123456789012345678"


def test_guard_rejects_extra_request_fields() -> None:
    request = _ExtraRelevanceRequest(
        bucket_id=3,
        group_handle="g1",
        vertical_name="Tech",
        instructions="policy",
        messages=(),
    )
    with pytest.raises(UnsafeRequestError):
        assert_safe_request(request)


def test_guard_rejects_backend_only_field_names() -> None:
    request = _LeakyRelevanceRequest(
        bucket_id=3,
        group_handle="g1",
        vertical_name="Tech",
        instructions="policy",
        messages=(),
    )
    with pytest.raises(UnsafeRequestError):
        assert_safe_request(request)


# --- adapter refuses before dispatch -------------------------------------


def test_adapter_refuses_an_unsafe_relevance_request_before_dispatch() -> None:
    transport = NoRequestTransport()
    adapter = _adapter(transport)

    with pytest.raises(UnsafeRequestError):
        adapter.classify(_relevance_request(content="hey <@123456789012345678>"))

    assert transport.requests == []
