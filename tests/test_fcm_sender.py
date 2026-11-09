"""FCM HTTP v1 sender behind ``PushSender`` (W4, EIR-04, FR-NOT-07, O11-2).

The sender is one module behind the protocol; ``backend.notification_delivery``
never imports it. The signing package is optional: importing either module and
running the core suite works without it, and a missing extra raises
``PushProviderNotConfigured`` at send time, never at import.

Payloads are Android-shaped (``message.android.collapse_key``) because the app
is Android-only for now. All transports and token providers here are fakes; the
suite makes no network call and never reads a real service account.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.config import Config, ConfigError
from backend.fcm_sender import (
    FCM_SERVICE_ACCOUNT_JSON_KEY,
    FcmPushSender,
    FcmResponse,
    PushProviderNotConfigured,
)
from backend.notification_delivery import PushDeliveryError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVICE_TOKEN = "fake-device-token-not-real"
BEARER_TOKEN = "fake-bearer-token-not-real"
SERVICE_ACCOUNT = json.dumps(
    {
        "project_id": "fake-project",
        "client_email": "fake@example.invalid",
        "private_key": "fake-private-key-not-real",
    }
)


class FakeTokenProvider:
    def __init__(self, token: str = BEARER_TOKEN) -> None:
        self._token = token
        self.service_accounts: list[str] = []

    def token(self, *, service_account_json: str) -> str:
        self.service_accounts.append(service_account_json)
        return self._token


class FakeFcmTransport:
    def __init__(self, responses: list[FcmResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> FcmResponse:
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


def _response(status: int = 200, payload: object = None) -> FcmResponse:
    return FcmResponse(
        status=status,
        headers={},
        body=json.dumps({} if payload is None else payload).encode("utf-8"),
    )


def _config(service_account: str = SERVICE_ACCOUNT) -> Config:
    return Config({FCM_SERVICE_ACCOUNT_JSON_KEY: service_account})


def _sender(
    responses: list[FcmResponse | BaseException],
    *,
    service_account: str = SERVICE_ACCOUNT,
    token_provider: FakeTokenProvider | None = None,
) -> tuple[FcmPushSender, FakeFcmTransport, FakeTokenProvider]:
    transport = FakeFcmTransport(responses)
    provider = FakeTokenProvider() if token_provider is None else token_provider
    sender = FcmPushSender(
        config=_config(service_account),
        transport=transport,
        token_provider=provider,
    )
    return sender, transport, provider


def _send(sender: FcmPushSender, *, deep_link: str | None = "/review/7") -> str | None:
    return sender.send(
        token=DEVICE_TOKEN,
        title="Proposal ready",
        body="Proposal ready for review",
        deep_link=deep_link,
        collapse_key="42",
    )


def _error(response: FcmResponse, *, deep_link: str | None = "/review/7") -> PushDeliveryError:
    sender, _, _ = _sender([response])
    with pytest.raises(PushDeliveryError) as captured:
        _send(sender, deep_link=deep_link)
    return captured.value


def test_payload_and_bearer_token_match_http_v1_contract() -> None:
    sender, transport, provider = _sender([_response(200, {"name": "projects/p/messages/1"})])

    result = _send(sender)

    assert result == "projects/p/messages/1"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["url"] == (
        "https://fcm.googleapis.com/v1/projects/fake-project/messages:send"
    )
    assert request["headers"]["Authorization"] == f"Bearer {BEARER_TOKEN}"
    assert request["headers"]["Content-Type"] == "application/json"
    assert provider.service_accounts == [SERVICE_ACCOUNT]
    assert json.loads(request["body"]) == {
        "message": {
            "token": DEVICE_TOKEN,
            "notification": {
                "title": "Proposal ready",
                "body": "Proposal ready for review",
            },
            "data": {"deep_link": "/review/7"},
            "android": {"collapse_key": "42"},
        }
    }


def test_payload_uses_an_empty_data_map_when_there_is_no_deep_link() -> None:
    sender, transport, _ = _sender([_response(200, {"name": "projects/p/messages/2"})])

    _send(sender, deep_link=None)

    payload = json.loads(transport.requests[0]["body"])
    assert payload["message"]["data"] == {}
    assert "ios" not in payload["message"]
    assert "apns" not in payload["message"]
    assert "platform" not in payload["message"]


def test_401_is_permanent_and_never_copies_the_provider_message() -> None:
    error = _error(
        _response(
            401,
            {
                "error": {
                    "status": "UNAUTHENTICATED",
                    "message": f"bad token {DEVICE_TOKEN} {BEARER_TOKEN}",
                }
            },
        )
    )

    assert error.permanent is True
    assert error.status_code == 401
    assert error.provider_code == "UNAUTHENTICATED"
    assert str(error) == "HTTP 401 UNAUTHENTICATED"
    assert DEVICE_TOKEN not in str(error)
    assert BEARER_TOKEN not in str(error)


def test_403_is_permanent() -> None:
    error = _error(_response(403, {"error": {"status": "PERMISSION_DENIED"}}))

    assert error.permanent is True
    assert error.status_code == 403
    assert error.provider_code == "PERMISSION_DENIED"


def test_429_is_retryable() -> None:
    error = _error(_response(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}))

    assert error.permanent is False
    assert error.status_code == 429
    assert error.provider_code == "RESOURCE_EXHAUSTED"


@pytest.mark.parametrize(
    ("status", "provider_code"),
    [(500, "INTERNAL"), (503, "UNAVAILABLE")],
)
def test_5xx_is_retryable(status: int, provider_code: str) -> None:
    error = _error(_response(status, {"error": {"status": provider_code}}))

    assert error.permanent is False
    assert error.status_code == status
    assert error.provider_code == provider_code


def test_any_other_4xx_is_permanent() -> None:
    error = _error(_response(400, {"error": {"status": "INVALID_ARGUMENT"}}))

    assert error.permanent is True
    assert error.status_code == 400
    assert error.provider_code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("body", [b"", b"{}", b"not-json"])
def test_malformed_success_response_fails_the_attempt(body: bytes) -> None:
    sender, _, _ = _sender([FcmResponse(status=200, headers={}, body=body)])

    with pytest.raises(PushDeliveryError) as captured:
        _send(sender)

    error = captured.value
    assert error.permanent is False
    assert error.provider_code == "MALFORMED_RESPONSE"
    assert error.status_code is None


def test_timeout_is_retryable_and_sanitised() -> None:
    sender, _, _ = _sender([TimeoutError(f"timeout {DEVICE_TOKEN}")])

    with pytest.raises(PushDeliveryError) as captured:
        _send(sender)

    error = captured.value
    assert error.permanent is False
    assert error.provider_code == "TRANSPORT_ERROR"
    assert DEVICE_TOKEN not in str(error)


def test_service_account_is_read_through_config_require() -> None:
    with pytest.raises(ConfigError):
        FcmPushSender(
            config=Config({}),
            transport=FakeFcmTransport([]),
            token_provider=FakeTokenProvider(),
        )


def test_missing_project_id_is_rejected_without_echoing_the_account() -> None:
    account = json.dumps({"client_email": "fake@example.invalid"})

    with pytest.raises(PushProviderNotConfigured) as captured:
        FcmPushSender(
            config=_config(account),
            transport=FakeFcmTransport([]),
            token_provider=FakeTokenProvider(),
        )

    assert "fake@example.invalid" not in str(captured.value)
    assert account not in str(captured.value)


def test_repr_does_not_expose_the_service_account_or_a_device_token() -> None:
    sender, _, _ = _sender([])

    text = repr(sender)

    assert "fake-private-key-not-real" not in text
    assert SERVICE_ACCOUNT not in text
    assert DEVICE_TOKEN not in text
    assert BEARER_TOKEN not in text


def test_notification_delivery_does_not_import_the_provider_module() -> None:
    script = (
        "import sys; import backend.notification_delivery;"
        " print('backend.fcm_sender' in sys.modules)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_core_modules_import_without_the_push_extra() -> None:
    script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == "google" or name.startswith("google."):
        raise ImportError("push extra is blocked for this test")
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import backend.notification_delivery
import backend.fcm_sender
print("IMPORTED")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IMPORTED"


def test_missing_extra_raises_push_provider_not_configured_at_send_time() -> None:
    script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == "google" or name.startswith("google."):
        raise ImportError("push extra is blocked for this test")
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
from backend.config import Config
from backend.fcm_sender import (
    FCM_SERVICE_ACCOUNT_JSON_KEY,
    FcmPushSender,
    FcmResponse,
    PushProviderNotConfigured,
)
class Transport:
    def request(self, *, url, headers, body, timeout):
        raise AssertionError("no network call is allowed")
sender = FcmPushSender(
    config=Config({FCM_SERVICE_ACCOUNT_JSON_KEY: '{"project_id": "fake-project"}'}),
    transport=Transport(),
)
try:
    sender.send(
        token="fake-device-token",
        title="t",
        body="b",
        deep_link=None,
        collapse_key="1",
    )
except PushProviderNotConfigured:
    print("NOT_CONFIGURED")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOT_CONFIGURED"
