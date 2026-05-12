"""Unit tests for PipedreamConnectOAuthProvider (WO #24)."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import OnboardingConnector
from alex_agent_runtime.services.oauth_provider import (
    OAuthProviderError,
    PipedreamConnectOAuthProvider,
    build_default_oauth_provider,
)


class _FakeConnectClient:
    """Minimal stand-in for ``PipedreamConnectClient``."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            "id": "token_id",
            "token": "ctok_test",
            "expires_at": "2026-05-12T11:00:00Z",
            "connect_link_url": "https://pipedream.com/_static/connect.html?token=ctok_test&connectLink=true",
        }
        self.calls: list[dict[str, Any]] = []

    async def create_connect_token(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def close(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        alex_pipedream_connect_project_id="proj_test",
        alex_pipedream_connect_client_id="cid",
        alex_pipedream_connect_client_secret="csecret",
        oauth_provider="pipedream_connect",
    )


@pytest.mark.asyncio
async def test_initiate_close_returns_link_with_app_query():
    fake = _FakeConnectClient()
    provider = PipedreamConnectOAuthProvider(_settings(), client=fake)
    tenant_id = uuid4()
    rep_id = uuid4()

    initiation = await provider.initiate(
        connector=OnboardingConnector.CLOSE,
        tenant_id=tenant_id,
        rep_id=rep_id,
    )

    assert initiation.state == "ctok_test"
    assert initiation.stub is False
    assert initiation.authorize_url.endswith("&app=close")
    assert fake.calls[0]["external_user_id"] == f"alex:{tenant_id}:{rep_id}:close"


@pytest.mark.asyncio
async def test_initiate_google_uses_gmail_first():
    """Google maps to (gmail, google_calendar); v1 prompts Gmail first."""
    fake = _FakeConnectClient()
    provider = PipedreamConnectOAuthProvider(_settings(), client=fake)

    initiation = await provider.initiate(
        connector=OnboardingConnector.GOOGLE,
        tenant_id=uuid4(),
        rep_id=uuid4(),
    )
    assert "&app=gmail" in initiation.authorize_url


def test_follow_up_app_slugs_for_google():
    assert PipedreamConnectOAuthProvider.follow_up_app_slugs(
        OnboardingConnector.GOOGLE
    ) == ("google_calendar",)
    assert (
        PipedreamConnectOAuthProvider.follow_up_app_slugs(OnboardingConnector.CLOSE)
        == ()
    )


def test_encode_decode_external_user_id_roundtrip():
    tenant_id = UUID("12345678-1234-5678-1234-567812345678")
    rep_id = UUID("11111111-2222-3333-4444-555555555555")
    encoded = PipedreamConnectOAuthProvider.encode_external_user_id(
        tenant_id=tenant_id,
        rep_id=rep_id,
        connector=OnboardingConnector.CLOSE,
    )
    decoded = PipedreamConnectOAuthProvider.decode_external_user_id(encoded)
    assert decoded == (tenant_id, rep_id, OnboardingConnector.CLOSE)


def test_decode_external_user_id_rejects_foreign_payloads():
    assert (
        PipedreamConnectOAuthProvider.decode_external_user_id("someone-elses-id")
        is None
    )
    assert (
        PipedreamConnectOAuthProvider.decode_external_user_id("alex:nope:nope:close")
        is None
    )
    assert (
        PipedreamConnectOAuthProvider.decode_external_user_id(
            "alex:12345678-1234-5678-1234-567812345678:"
            "11111111-2222-3333-4444-555555555555:unknown_connector"
        )
        is None
    )


@pytest.mark.asyncio
async def test_exchange_raises_in_connect_mode():
    fake = _FakeConnectClient()
    provider = PipedreamConnectOAuthProvider(_settings(), client=fake)
    with pytest.raises(OAuthProviderError):
        await provider.exchange(
            connector=OnboardingConnector.CLOSE,
            state="x",
            code="y",
            tenant_id=uuid4(),
            rep_id=uuid4(),
        )


def test_builder_returns_connect_provider_when_mode_set():
    provider = build_default_oauth_provider(_settings())
    assert isinstance(provider, PipedreamConnectOAuthProvider)


def test_builder_refuses_connect_mode_without_credentials():
    bare = Settings(
        oauth_provider="pipedream_connect",
        alex_pipedream_connect_project_id="",
        alex_pipedream_connect_client_id="",
        alex_pipedream_connect_client_secret="",
    )
    with pytest.raises(OAuthProviderError):
        build_default_oauth_provider(bare)
