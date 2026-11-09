"""FCM HTTP v1 sender behind the ``PushSender`` protocol (W4, EIR-04).

This is the only module that knows the Firebase endpoint or imports the
optional signing package. ``backend.notification_delivery`` imports the
protocol and never this module, so the core suite runs on a clean install with
no ``push`` extra. A missing extra raises :class:`PushProviderNotConfigured` at
send time, never at import time.

Payloads are Android-shaped because the app is Android-only for now:
``message.token``, ``message.notification.title/body``,
``message.data.deep_link`` and ``message.android.collapse_key``. No iOS, APNs or
platform field is emitted. The service-account JSON is read through
:meth:`Config.require`; it is never logged, stored in the database or copied
into an error. Errors carry only the HTTP status and a provider code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from backend.config import Config
from backend.notification_delivery import PushDeliveryError

FCM_SERVICE_ACCOUNT_JSON_KEY = "FCM_SERVICE_ACCOUNT_JSON"
FCM_PROJECT_ID_KEY = "FCM_PROJECT_ID"
FCM_BASE_URL = "https://fcm.googleapis.com"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class PushProviderNotConfigured(Exception):
    """The optional ``push`` extra is not installed or is unusable.

    It is permanent: retrying cannot install a package or fix a missing
    service account. The class deliberately carries no account content.
    """

    permanent = True
    provider_code = "PROVIDER_NOT_CONFIGURED"


@dataclass(frozen=True)
class FcmResponse:
    """The transport result; a 4xx/5xx is returned, not raised."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class FcmTransport(Protocol):
    """The only network seam; tests inject a fake implementation."""

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> FcmResponse: ...


class TokenProvider(Protocol):
    """The signing seam: a fake provider keeps tests offline."""

    def token(self, *, service_account_json: str) -> str: ...


class UrllibFcmTransport:
    """Default stdlib transport; no third-party HTTP dependency."""

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> FcmResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return FcmResponse(
                    status=int(response.status),
                    headers=dict(response.headers),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return FcmResponse(
                status=int(error.code),
                headers=dict(error.headers or {}),
                body=error.read(),
            )


class GoogleServiceAccountTokenProvider:
    """Lazy real signer; importing it never imports ``google``."""

    def token(self, *, service_account_json: str) -> str:
        try:
            from google.auth.transport.requests import Request  # type: ignore[import-not-found]
            from google.oauth2 import service_account  # type: ignore[import-not-found]
        except ImportError:
            raise PushProviderNotConfigured(
                "FCM push extra is not installed"
            ) from None

        try:
            info = json.loads(service_account_json)
        except json.JSONDecodeError:
            raise PushProviderNotConfigured(
                "FCM service account JSON is invalid"
            ) from None
        if not isinstance(info, dict):
            raise PushProviderNotConfigured(
                "FCM service account JSON is invalid"
            ) from None

        try:
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[FCM_SCOPE]
            )
            credentials.refresh(Request())
        except Exception:  # noqa: BLE001 - provider text may carry the key
            raise PushProviderNotConfigured("FCM signing failed") from None

        token = credentials.token
        if not isinstance(token, str) or token.strip() == "":
            raise PushProviderNotConfigured("FCM signing returned no token")
        return token


def _project_id(service_account_json: str) -> str:
    try:
        payload = json.loads(service_account_json)
    except json.JSONDecodeError:
        raise PushProviderNotConfigured(
            "FCM service account JSON is invalid"
        ) from None
    if not isinstance(payload, dict):
        raise PushProviderNotConfigured("FCM service account JSON is invalid")
    project = payload.get("project_id")
    if not isinstance(project, str) or project.strip() == "":
        raise PushProviderNotConfigured("FCM project id is not configured")
    return project.strip()


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _message_name(body: bytes) -> str | None:
    payload = _json_object(body)
    if payload is None:
        return None
    name = payload.get("name")
    if not isinstance(name, str) or name.strip() == "":
        return None
    return name


def _safe_code(value: str) -> str | None:
    """Keep only a short provider-code-shaped token, never provider prose."""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    cleaned = "".join(character for character in value.strip() if character in allowed)
    return cleaned[:64] or None


def _provider_code(body: bytes) -> str | None:
    payload = _json_object(body)
    if payload is None:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status")
    if isinstance(status, str) and status.strip():
        return _safe_code(status)
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict):
                reason = detail.get("reason")
                if isinstance(reason, str) and reason.strip():
                    return _safe_code(reason)
    return None


class FcmPushSender:
    """FCM HTTP v1 implementation of ``PushSender`` (one module, no core import)."""

    def __init__(
        self,
        *,
        config: Config,
        transport: FcmTransport | None = None,
        token_provider: TokenProvider | None = None,
        base_url: str = FCM_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._service_account = config.require(FCM_SERVICE_ACCOUNT_JSON_KEY)
        configured_project = config.get(FCM_PROJECT_ID_KEY)
        if configured_project is None or configured_project.strip() == "":
            self._project_id = _project_id(self._service_account)
        else:
            self._project_id = configured_project.strip()
        self._transport = (
            UrllibFcmTransport() if transport is None else transport
        )
        self._token_provider = (
            GoogleServiceAccountTokenProvider()
            if token_provider is None
            else token_provider
        )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:
        # The service account, token and device token are deliberately absent.
        return f"FcmPushSender(project_id={self._project_id!r})"

    def send(
        self,
        *,
        token: str,
        title: str,
        body: str,
        deep_link: str | None,
        collapse_key: str,
    ) -> str | None:
        try:
            bearer = self._token_provider.token(
                service_account_json=self._service_account
            )
        except PushProviderNotConfigured:
            raise
        except Exception:  # noqa: BLE001 - provider text may carry the key
            raise PushDeliveryError(
                permanent=False, provider_code="TOKEN_ERROR"
            ) from None
        if not isinstance(bearer, str) or bearer.strip() == "":
            raise PushDeliveryError(
                permanent=False, provider_code="TOKEN_ERROR"
            )

        payload = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {} if deep_link is None else {"deep_link": deep_link},
                "android": {"collapse_key": collapse_key},
            }
        }
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }
        try:
            response = self._transport.request(
                url=(
                    f"{self._base_url}/v1/projects/{self._project_id}"
                    "/messages:send"
                ),
                headers=headers,
                body=json.dumps(payload).encode("utf-8"),
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001 - a transport failure is retryable
            raise PushDeliveryError(
                permanent=False, provider_code="TRANSPORT_ERROR"
            ) from None

        if 200 <= response.status < 300:
            name = _message_name(response.body)
            if name is None:
                raise PushDeliveryError(
                    permanent=False, provider_code="MALFORMED_RESPONSE"
                )
            return name

        permanent = not (
            response.status == 429 or 500 <= response.status <= 599
        )
        raise PushDeliveryError(
            permanent=permanent,
            status_code=response.status,
            provider_code=_provider_code(response.body),
        )
