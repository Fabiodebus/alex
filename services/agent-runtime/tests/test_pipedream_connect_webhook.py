"""Tests for the ``/webhooks/pipedream-connect`` receiver (WO #24)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from alex_agent_runtime.main import create_app
from alex_agent_runtime.schemas import OnboardingConnector
from alex_agent_runtime.services.oauth_provider import PipedreamConnectOAuthProvider

_SECRET = "test-pdc-webhook-secret"


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.success_calls: list[dict[str, Any]] = []
        self.failure_calls: list[dict[str, Any]] = []
        self._return_completion: Any = object()  # sentinel for "no follow-up"

    async def complete_via_pipedream_connect(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
        account_id: str,
        app_slug: str,
        is_primary_slug: bool,
    ):
        self.success_calls.append(
            {
                "tenant_id": tenant_id,
                "rep_id": rep_id,
                "connector": connector,
                "account_id": account_id,
                "app_slug": app_slug,
                "is_primary_slug": is_primary_slug,
            }
        )
        if not is_primary_slug:
            return None
        return _FakeCompletion(connector=connector, success=True)

    async def fail_via_pipedream_connect(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
        reason: str,
    ):
        self.failure_calls.append(
            {
                "tenant_id": tenant_id,
                "rep_id": rep_id,
                "connector": connector,
                "reason": reason,
            }
        )
        return _FakeCompletion(connector=connector, success=False)


class _FakeCompletion:
    def __init__(self, *, connector: OnboardingConnector, success: bool) -> None:
        self.connector = connector
        self.success = success


class _FakeFlow:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def on_connector_completed(self, *, completion):
        self.calls.append(completion)


def _sign(body: bytes, *, secret: str = _SECRET, ts: int | None = None) -> str:
    timestamp = str(ts if ts is not None else int(time.time()))
    signed_payload = f"{timestamp}.{body.decode('utf-8')}"
    mac = hmac.new(
        secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={mac}"


def _event(
    *,
    event: str,
    external_id: str,
    app_slug: str,
    account_id: str = "apn_test_1",
) -> dict[str, Any]:
    return {
        "event": event,
        "connect_token": "ctok_x",
        "environment": "production",
        "account": {
            "id": account_id,
            "name": "rep@example.com",
            "external_id": external_id,
            "healthy": True,
            "app": {"name_slug": app_slug, "name": app_slug.title()},
        },
    }


async def _post(client: AsyncClient, *, body: dict[str, Any], signed: bool = True):
    raw = json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {"content-type": "application/json"}
    if signed:
        headers["x-pd-signature"] = _sign(raw)
    return await client.post("/webhooks/pipedream-connect", content=raw, headers=headers)


@pytest.fixture
def configured_app(monkeypatch):
    """Spin up the app with the webhook secret + fake orchestrator/flow."""
    monkeypatch.setenv("ALEX_PIPEDREAM_CONNECT_WEBHOOK_SECRET", _SECRET)
    app = create_app()
    orchestrator = _FakeOrchestrator()
    flow = _FakeFlow()
    # We don't need full lifespan startup for these tests — wire just
    # what the route reads off app.state.
    app.state.oauth_orchestrator = orchestrator
    app.state.onboarding_flow = flow
    from alex_agent_runtime.config import get_settings

    get_settings.cache_clear()
    app.state.settings = get_settings()
    return app, orchestrator, flow


@pytest.mark.asyncio
async def test_success_for_primary_slug_routes_through_orchestrator(configured_app):
    app, orchestrator, flow = configured_app
    tenant_id = uuid4()
    rep_id = uuid4()
    external_id = PipedreamConnectOAuthProvider.encode_external_user_id(
        tenant_id=tenant_id, rep_id=rep_id, connector=OnboardingConnector.CLOSE
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await _post(
            client,
            body=_event(
                event="CONNECTION_SUCCESS",
                external_id=external_id,
                app_slug="close",
                account_id="apn_close_1",
            ),
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "connector": "close",
        "app_slug": "close",
        "primary": True,
    }
    assert len(orchestrator.success_calls) == 1
    call = orchestrator.success_calls[0]
    assert call["tenant_id"] == tenant_id
    assert call["rep_id"] == rep_id
    assert call["account_id"] == "apn_close_1"
    assert call["is_primary_slug"] is True
    # Flow notified about the connector completing.
    assert len(flow.calls) == 1


@pytest.mark.asyncio
async def test_success_for_follow_up_slug_does_not_advance_flow(configured_app):
    """Calendar success after Gmail must not re-fire onboarding completion."""
    app, orchestrator, flow = configured_app
    tenant_id = uuid4()
    rep_id = uuid4()
    external_id = PipedreamConnectOAuthProvider.encode_external_user_id(
        tenant_id=tenant_id, rep_id=rep_id, connector=OnboardingConnector.GOOGLE
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await _post(
            client,
            body=_event(
                event="CONNECTION_SUCCESS",
                external_id=external_id,
                app_slug="google_calendar",
                account_id="apn_cal_1",
            ),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["primary"] is False
    assert orchestrator.success_calls[0]["is_primary_slug"] is False
    assert flow.calls == [], "follow-up account should not re-trigger flow advancement"


@pytest.mark.asyncio
async def test_failure_event_flips_connector_to_failed(configured_app):
    app, orchestrator, flow = configured_app
    tenant_id = uuid4()
    rep_id = uuid4()
    external_id = PipedreamConnectOAuthProvider.encode_external_user_id(
        tenant_id=tenant_id, rep_id=rep_id, connector=OnboardingConnector.CLOSE
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await _post(
            client,
            body=_event(
                event="CONNECTION_ERROR",
                external_id=external_id,
                app_slug="close",
            ),
        )

    assert resp.status_code == 200
    assert resp.json()["result"] == "failed"
    assert len(orchestrator.failure_calls) == 1
    assert orchestrator.failure_calls[0]["reason"] == "connection_error"
    assert len(flow.calls) == 1


@pytest.mark.asyncio
async def test_unsigned_request_is_rejected_when_secret_set(configured_app):
    app, *_ = configured_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/webhooks/pipedream-connect",
            json=_event(
                event="CONNECTION_SUCCESS",
                external_id="alex:00000000-0000-0000-0000-000000000001:"
                "00000000-0000-0000-0000-000000000002:close",
                app_slug="close",
            ),
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bad_signature_is_rejected(configured_app):
    app, *_ = configured_app
    body = json.dumps(_event(
        event="CONNECTION_SUCCESS",
        external_id="alex:00000000-0000-0000-0000-000000000001:"
                    "00000000-0000-0000-0000-000000000002:close",
        app_slug="close",
    )).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-pd-signature": f"t={int(time.time())},v1=" + "0" * 64,
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/webhooks/pipedream-connect", content=body, headers=headers
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stale_timestamp_is_rejected(configured_app):
    app, *_ = configured_app
    body = json.dumps(_event(
        event="CONNECTION_SUCCESS",
        external_id="alex:00000000-0000-0000-0000-000000000001:"
                    "00000000-0000-0000-0000-000000000002:close",
        app_slug="close",
    )).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-pd-signature": _sign(body, ts=int(time.time()) - 3600),
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/webhooks/pipedream-connect", content=body, headers=headers
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_foreign_external_id_is_ack_but_ignored(configured_app):
    app, orchestrator, flow = configured_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await _post(
            client,
            body=_event(
                event="CONNECTION_SUCCESS",
                external_id="not-an-alex-id",
                app_slug="close",
            ),
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert orchestrator.success_calls == []
    assert orchestrator.failure_calls == []


@pytest.mark.asyncio
async def test_unknown_app_slug_is_ack_but_ignored(configured_app):
    app, orchestrator, flow = configured_app
    tenant_id = uuid4()
    rep_id = uuid4()
    external_id = PipedreamConnectOAuthProvider.encode_external_user_id(
        tenant_id=tenant_id, rep_id=rep_id, connector=OnboardingConnector.CLOSE
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await _post(
            client,
            body=_event(
                event="CONNECTION_SUCCESS",
                external_id=external_id,
                app_slug="dropbox",  # not what we asked the rep to connect
            ),
        )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "unexpected_app_slug"
    assert orchestrator.success_calls == []
