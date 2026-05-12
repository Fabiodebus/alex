"""Unit tests for PipedreamConnectClient (WO #24)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.services.pipedream_connect_client import (
    PipedreamConnectClient,
    PipedreamConnectError,
    build_pipedream_connect_client,
)


def _settings() -> Settings:
    return Settings(
        alex_pipedream_connect_project_id="proj_test",
        alex_pipedream_connect_client_id="cid",
        alex_pipedream_connect_client_secret="csecret",
        alex_pipedream_connect_api_base="https://api.pipedream.test/v1",
        alex_pipedream_connect_environment="production",
    )


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_create_connect_token_uses_client_credentials_then_calls_endpoint():
    calls: list[tuple[str, str, dict[str, str], object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                str(request.url),
                dict(request.headers),
                json.loads(request.content.decode()) if request.content else None,
            )
        )
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "tok_abc",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/connect/proj_test/tokens"):
            return httpx.Response(
                200,
                json={
                    "id": "token_id_1",
                    "token": "ctok_xyz",
                    "expires_at": "2026-05-12T11:00:00Z",
                    "connect_link_url": "https://pipedream.com/_static/connect.html?token=ctok_xyz&connectLink=true",
                },
            )
        return httpx.Response(404)

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PipedreamConnectClient(_settings(), client=http)
        result = await client.create_connect_token(
            external_user_id="alex:t:r:close"
        )

    assert result["token"] == "ctok_xyz"
    assert calls[0][1].endswith("/oauth/token")
    assert calls[1][1].endswith("/connect/proj_test/tokens")
    assert calls[1][2].get("authorization") == "Bearer tok_abc"
    assert calls[1][2].get("x-pd-environment") == "production"
    assert calls[1][3] == {"external_user_id": "alex:t:r:close"}


@pytest.mark.asyncio
async def test_access_token_is_cached_and_reused():
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path.endswith("/oauth/token"):
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "tok", "expires_in": 3600},
            )
        return httpx.Response(200, json={"data": []})

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PipedreamConnectClient(_settings(), client=http)
        await client.list_accounts(external_user_id="alex:t:r:close")
        await client.list_accounts(external_user_id="alex:t:r:close")
        await client.list_accounts(external_user_id="alex:t:r:close")

    assert token_calls == 1, "expected client_credentials exchange to be cached"


@pytest.mark.asyncio
async def test_401_refreshes_token_and_retries_once():
    sequence: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            sequence.append("token")
            return httpx.Response(
                200,
                json={"access_token": f"tok-{len(sequence)}", "expires_in": 3600},
            )
        sequence.append("call")
        # First call 401, second call 200.
        if sequence.count("call") == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"data": []})

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PipedreamConnectClient(_settings(), client=http)
        result = await client.list_accounts()

    assert result == []
    assert sequence == ["token", "call", "token", "call"], sequence


@pytest.mark.asyncio
async def test_non_401_4xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={"access_token": "tok", "expires_in": 3600},
            )
        return httpx.Response(403, json={"error": "forbidden"})

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = PipedreamConnectClient(_settings(), client=http)
        with pytest.raises(PipedreamConnectError) as exc_info:
            await client.list_accounts()
    assert exc_info.value.status == 403


def test_build_connect_link_url_appends_app_query():
    url = PipedreamConnectClient.build_connect_link_url(
        base_connect_link_url="https://pipedream.com/_static/connect.html?token=ctok_x&connectLink=true",
        app_slug="gmail",
    )
    assert url.endswith("&app=gmail")
    # Sanity: when no slug, return base unchanged.
    same = PipedreamConnectClient.build_connect_link_url(
        base_connect_link_url="https://example.com/x", app_slug=None
    )
    assert same == "https://example.com/x"


def test_builder_returns_none_without_credentials():
    bare = Settings(
        alex_pipedream_connect_project_id="",
        alex_pipedream_connect_client_id="",
        alex_pipedream_connect_client_secret="",
    )
    assert build_pipedream_connect_client(bare) is None


def test_builder_returns_client_when_configured():
    s = _settings()
    client = build_pipedream_connect_client(s)
    assert isinstance(client, PipedreamConnectClient)
