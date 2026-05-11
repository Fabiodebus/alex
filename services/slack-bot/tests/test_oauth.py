from __future__ import annotations

import httpx
import pytest

from alex_slack_bot.config import Settings
from alex_slack_bot.services.oauth_providers import (
    InvalidStateError,
    build_auth_url,
    exchange_code,
)
from uuid import uuid4


def _settings_with_state(secret: str = "oauth-state-secret") -> Settings:
    return Settings(
        oauth_state_secret=secret,
        oauth_google_client_id="goog-client",
        oauth_google_client_secret="goog-secret",
        oauth_google_redirect_uri="https://alex.test/oauth/callback",
    )


def test_build_auth_url_includes_signed_state(monkeypatch):
    monkeypatch.setattr(
        "alex_slack_bot.services.oauth_providers.get_settings",
        lambda: _settings_with_state(),
    )
    tenant_id = uuid4()
    rep_id = uuid4()
    url = build_auth_url(provider="google", tenant_id=tenant_id, rep_id=rep_id)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=" in url
    assert "client_id=goog-client" in url
    assert "redirect_uri=https%3A%2F%2Falex.test%2Foauth%2Fcallback" in url


@pytest.mark.asyncio
async def test_exchange_code_round_trips_state_and_token(monkeypatch):
    settings = _settings_with_state()
    monkeypatch.setattr(
        "alex_slack_bot.services.oauth_providers.get_settings",
        lambda: settings,
    )
    tenant_id = uuid4()
    rep_id = uuid4()
    # Build a real state token so the encode/decode signatures align.
    url = build_auth_url(provider="google", tenant_id=tenant_id, rep_id=rep_id)
    state = url.split("state=", 1)[1]

    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.fake",
                "refresh_token": "1//fake",
                "expires_in": 3599,
                "scope": "openid email https://www.googleapis.com/auth/gmail.send",
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        result = await exchange_code(provider="google", code="auth-code", state=state, http=http)

    assert captured["url"] == "https://oauth2.googleapis.com/token"
    assert "code=auth-code" in captured["body"]
    assert result["token"]["access_token"] == "ya29.fake"
    assert result["token"]["tenant_id"] == str(tenant_id)
    assert result["token"]["rep_id"] == str(rep_id)
    assert "gmail.send" in " ".join(result["token"]["scopes"])


@pytest.mark.asyncio
async def test_exchange_code_rejects_tampered_state(monkeypatch):
    settings = _settings_with_state()
    monkeypatch.setattr(
        "alex_slack_bot.services.oauth_providers.get_settings",
        lambda: settings,
    )
    tenant_id = uuid4()
    rep_id = uuid4()
    url = build_auth_url(provider="google", tenant_id=tenant_id, rep_id=rep_id)
    state = url.split("state=", 1)[1]
    tampered = state[:-2] + ("aa" if state[-2:] != "aa" else "bb")
    with pytest.raises(InvalidStateError):
        await exchange_code(provider="google", code="x", state=tampered)


@pytest.mark.asyncio
async def test_oauth_start_route_redirects_to_provider(client, monkeypatch):
    settings = _settings_with_state()
    monkeypatch.setattr(
        "alex_slack_bot.services.oauth_providers.get_settings",
        lambda: settings,
    )
    response = await client.get(
        "/oauth/start",
        params={"provider": "google", "tenant_id": str(uuid4()), "rep_id": str(uuid4())},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://accounts.google.com/")
