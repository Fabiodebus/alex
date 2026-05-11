"""OAuth providers — auth URL builder + token exchange.

Same shape as the slack-bot equivalent. State is HMAC-signed so the
callback handler can verify it wasn't tampered with and pull out the
``tenant_id`` / ``rep_id`` without a server-side session store.
"""
from __future__ import annotations

import base64
import hmac
import json
import secrets
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID

import httpx

from ..config import Settings, get_settings


class OAuthError(Exception):
    pass


class InvalidStateError(OAuthError):
    pass


class UnknownProviderError(OAuthError):
    pass


@dataclass(slots=True, frozen=True)
class ProviderConfig:
    name: str
    auth_url: str
    token_url: str
    default_scopes: tuple[str, ...]

    def client_id(self, settings: Settings) -> str:
        return getattr(settings, f"oauth_{self.name}_client_id", "") or ""

    def client_secret(self, settings: Settings) -> str:
        return getattr(settings, f"oauth_{self.name}_client_secret", "") or ""

    def redirect_uri(self, settings: Settings) -> str:
        return getattr(settings, f"oauth_{self.name}_redirect_uri", "") or ""


PROVIDERS: dict[str, ProviderConfig] = {
    "google": ProviderConfig(
        name="google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        default_scopes=(
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ),
    ),
}


def build_auth_url(
    *,
    provider: str,
    tenant_id: UUID,
    rep_id: UUID,
    scopes: list[str] | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    config = _get_provider(provider)
    state = _encode_state(
        secret=settings.oauth_state_secret or settings.alex_webhook_secret,
        tenant_id=tenant_id,
        rep_id=rep_id,
        provider=provider,
    )
    params = {
        "client_id": config.client_id(settings),
        "redirect_uri": config.redirect_uri(settings),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": " ".join(scopes or config.default_scopes),
        "state": state,
    }
    return f"{config.auth_url}?{urlencode(params)}"


async def exchange_code(
    *,
    provider: str,
    code: str,
    state: str,
    settings: Settings | None = None,
    http: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    config = _get_provider(provider)
    decoded = _decode_state(
        secret=settings.oauth_state_secret or settings.alex_webhook_secret,
        state=state,
    )
    if decoded["provider"] != provider:
        raise InvalidStateError(f"state provider {decoded['provider']!r} does not match {provider!r}")
    payload = {
        "client_id": config.client_id(settings),
        "client_secret": config.client_secret(settings),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": config.redirect_uri(settings),
    }
    owned = http is None
    client = http or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.post(config.token_url, data=payload)
    finally:
        if owned:
            await client.aclose()
    response.raise_for_status()
    body = response.json()
    return {
        "state": decoded,
        "token": {
            "tenant_id": decoded["tenant_id"],
            "rep_id": decoded["rep_id"],
            "source": provider,
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "expires_in": body.get("expires_in"),
            "scopes": (body.get("scope") or "").split(" ") if body.get("scope") else [],
        },
    }


def _get_provider(name: str) -> ProviderConfig:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise UnknownProviderError(f"unknown OAuth provider: {name!r}") from exc


def _encode_state(*, secret: str, tenant_id: UUID, rep_id: UUID, provider: str) -> str:
    if not secret:
        raise OAuthError("oauth_state_secret (or alex_webhook_secret) must be set to mint OAuth state")
    raw = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "rep_id": str(rep_id),
            "provider": provider,
            "nonce": secrets.token_urlsafe(8),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def _decode_state(*, secret: str, state: str) -> dict[str, str]:
    if not secret:
        raise InvalidStateError("oauth_state_secret missing on callback")
    try:
        raw_b64, sig_b64 = state.split(".", 1)
        raw = _b64_decode(raw_b64)
        sig = _b64_decode(sig_b64)
    except (ValueError, TypeError) as exc:
        raise InvalidStateError("malformed state") from exc
    expected_sig = hmac.new(secret.encode("utf-8"), raw, sha256).digest()
    if not hmac.compare_digest(expected_sig, sig):
        raise InvalidStateError("state signature mismatch")
    return json.loads(raw.decode("utf-8"))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
