"""OTP sender interface and Discord DM sender (W5, FR-ROS-03, O11-1 (d)).

The OTP is delivered as a Discord DM from the ASEC bot to the roster Discord
ID. The code is a secret: it may appear in the DM request body, but never in a
log line, an exception, a ``repr`` or a sanitised outcome. A failed DM is
recorded and returns the same shape as a delivered one, so Phase 12's sign-in
response cannot reveal whether the DM arrived.

Every transport here is scripted; the suite makes no network call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from backend.config import DISCORD_BOT_TOKEN_KEY, Config
from backend.discord_adapter import HttpResponse
from backend.notification_delivery import (
    DevelopmentOtpSender,
    DiscordDmOtpSender,
    OtpSenderNotConfigured,
    RecordingOtpSender,
    require_otp_sender,
)

FAKE_BOT_TOKEN = "fake-bot-token-not-real"
FAKE_CODE = "FAKE-OTP-CODE-123456"
FAKE_DISCORD_ID = "987654321012345678"


class FakePostTransport:
    """Scripted POST transport; records every request, never touches the network."""

    def __init__(self, responses: list[HttpResponse | BaseException]) -> None:
        self.responses = list(responses)
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
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _json_response(status: int = 200, payload: object = None) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={},
        body=json.dumps({} if payload is None else payload).encode("utf-8"),
    )


def _config() -> Config:
    return Config({DISCORD_BOT_TOKEN_KEY: FAKE_BOT_TOKEN})


def _sender(responses: list[HttpResponse | BaseException]) -> tuple[
    DiscordDmOtpSender, FakePostTransport
]:
    transport = FakePostTransport(responses)
    return (
        DiscordDmOtpSender(config=_config(), transport=transport),
        transport,
    )


def _channel_response() -> HttpResponse:
    return _json_response(200, {"id": "111222333444555666"})


def _message_response() -> HttpResponse:
    return _json_response(200, {"id": "777888999000111222"})


def test_discord_dm_opens_a_dm_channel_and_sends_one_message() -> None:
    sender, transport = _sender([_channel_response(), _message_response()])

    result = sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert result is None
    assert sender.outcomes == ("SENT",)
    assert len(transport.requests) == 2

    channel_request, message_request = transport.requests
    assert channel_request["url"].endswith("/users/@me/channels")
    assert json.loads(channel_request["body"]) == {"recipient_id": FAKE_DISCORD_ID}
    assert channel_request["headers"]["Authorization"] == f"Bot {FAKE_BOT_TOKEN}"

    assert message_request["url"].endswith("/channels/111222333444555666/messages")
    assert json.loads(message_request["body"]) == {
        "content": f"Your ASEC sign-in code is {FAKE_CODE}"
    }
    assert message_request["headers"]["Authorization"] == f"Bot {FAKE_BOT_TOKEN}"


def test_failed_dm_returns_the_same_shape_and_a_sanitised_outcome() -> None:
    failure_body = {
        "message": f"cannot DM {FAKE_CODE} with token {FAKE_BOT_TOKEN}",
        "code": 50007,
    }
    sender, transport = _sender([_json_response(403, failure_body)])

    result = sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert result is None
    assert sender.outcomes == ("HTTP 403",)
    assert len(transport.requests) == 1
    assert FAKE_CODE not in sender.outcomes[0]
    assert FAKE_BOT_TOKEN not in sender.outcomes[0]


def test_transport_failure_never_raises_or_leaks_the_token_or_code() -> None:
    sender, _ = _sender([OSError(f"connection failed for {FAKE_CODE} {FAKE_BOT_TOKEN}")])

    result = sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert result is None
    assert sender.outcomes == ("TRANSPORT_ERROR",)
    assert FAKE_CODE not in sender.outcomes[0]
    assert FAKE_BOT_TOKEN not in sender.outcomes[0]


def test_malformed_channel_response_does_not_send_the_message() -> None:
    sender, transport = _sender([_json_response(200, {})])

    result = sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert result is None
    assert sender.outcomes == ("MALFORMED_RESPONSE",)
    assert len(transport.requests) == 1


def test_non_numeric_channel_id_is_rejected_before_the_message_post() -> None:
    sender, transport = _sender([_json_response(200, {"id": "../../evil"})])

    result = sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert result is None
    assert sender.outcomes == ("MALFORMED_RESPONSE",)
    assert len(transport.requests) == 1


def test_otp_code_and_bot_token_never_appear_in_repr_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="asec.notification_delivery")
    sender, _ = _sender([_json_response(403, {"message": FAKE_CODE})])

    sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert FAKE_CODE not in repr(sender)
    assert FAKE_BOT_TOKEN not in repr(sender)
    assert FAKE_CODE not in caplog.text
    assert FAKE_BOT_TOKEN not in caplog.text


def test_recording_sender_records_without_network_and_keeps_code_out_of_repr() -> None:
    sender = RecordingOtpSender()

    sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert sender.sent == ((FAKE_DISCORD_ID, FAKE_CODE),)
    assert FAKE_CODE not in repr(sender)
    assert FAKE_DISCORD_ID not in repr(sender)


def test_development_sender_records_instead_of_sending() -> None:
    sender = DevelopmentOtpSender()

    sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert sender.sent == ((FAKE_DISCORD_ID, FAKE_CODE),)
    assert FAKE_CODE not in repr(sender)


def test_development_sender_refuses_to_run_in_production() -> None:
    sender = DevelopmentOtpSender(production=True)

    with pytest.raises(OtpSenderNotConfigured):
        sender.send(discord_id=FAKE_DISCORD_ID, code=FAKE_CODE)

    assert sender.sent == ()


def test_require_otp_sender_raises_when_no_sender_is_configured() -> None:
    with pytest.raises(OtpSenderNotConfigured):
        require_otp_sender(None)

    recording = RecordingOtpSender()
    assert require_otp_sender(recording) is recording


def test_discord_dm_rejects_blank_identity_without_echoing_it() -> None:
    sender, transport = _sender([_channel_response(), _message_response()])

    with pytest.raises(ValueError):
        sender.send(discord_id="   ", code=FAKE_CODE)

    assert transport.requests == []
    assert sender.outcomes == ()
