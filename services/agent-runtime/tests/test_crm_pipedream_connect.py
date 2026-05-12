"""Tests for PipedreamConnect CRM fetch/write clients (WO #24)."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    CRMFetchRequest,
    CRMNote,
    CRMPlatform,
    CRMRecordKind,
    CRMWriteRequest,
    CRMWriteStatus,
    FieldUpdate,
    ValidatedFieldUpdate,
    OnboardingConnector,
)
from alex_agent_runtime.services.connect_account_resolver import (
    StaticConnectAccountResolver,
)
from alex_agent_runtime.services.crm_fetch_client import (
    PipedreamConnectCRMFetchClient,
)
from alex_agent_runtime.services.crm_write_client import (
    PipedreamConnectCRMWriteClient,
)


class _FakeConnectClient:
    """Records proxy_request calls and returns a canned response."""

    def __init__(self, *, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"statusCode": 200, "body": {}, "headers": {}}
        self.calls: list[dict[str, Any]] = []

    async def proxy_request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def close(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        alex_pipedream_connect_project_id="proj_test",
        alex_pipedream_connect_client_id="cid",
        alex_pipedream_connect_client_secret="csecret",
        crm_fetch_provider="pipedream_connect",
        crm_write_provider="pipedream_connect",
    )


@pytest.mark.asyncio
async def test_fetch_close_opportunity_routes_through_proxy():
    tenant_id = uuid4()
    resolver = StaticConnectAccountResolver(
        {(tenant_id, OnboardingConnector.CLOSE): "apn_close_1"}
    )
    fake = _FakeConnectClient(
        response={
            "statusCode": 200,
            "body": {"id": "oppo_42", "status_label": "Active"},
            "headers": {},
        }
    )
    client = PipedreamConnectCRMFetchClient(
        _settings(), connect_client=fake, resolver=resolver
    )

    result = await client.fetch(
        CRMFetchRequest(
            tenant_id=tenant_id,
            platform=CRMPlatform.CLOSE,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="oppo_42",
        )
    )

    assert result == {"id": "oppo_42", "status_label": "Active"}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["account_id"] == "apn_close_1"
    assert call["url"] == "https://api.close.com/api/v1/opportunity/oppo_42/"
    assert call["method"] == "GET"


@pytest.mark.asyncio
async def test_fetch_returns_none_when_no_account_connected():
    """Solo tenant with no Close connection should fetch None, not error."""
    resolver = StaticConnectAccountResolver({})  # empty mapping
    fake = _FakeConnectClient()
    client = PipedreamConnectCRMFetchClient(
        _settings(), connect_client=fake, resolver=resolver
    )

    result = await client.fetch(
        CRMFetchRequest(
            tenant_id=uuid4(),
            platform=CRMPlatform.CLOSE,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="oppo_x",
        )
    )

    assert result is None
    assert fake.calls == [], "proxy should not be called without account_id"


@pytest.mark.asyncio
async def test_fetch_handles_upstream_404_as_none():
    tenant_id = uuid4()
    resolver = StaticConnectAccountResolver(
        {(tenant_id, OnboardingConnector.CLOSE): "apn_close_1"}
    )
    fake = _FakeConnectClient(
        response={"statusCode": 404, "body": {"error": "not_found"}, "headers": {}}
    )
    client = PipedreamConnectCRMFetchClient(
        _settings(), connect_client=fake, resolver=resolver
    )

    result = await client.fetch(
        CRMFetchRequest(
            tenant_id=tenant_id,
            platform=CRMPlatform.CLOSE,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="missing",
        )
    )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_parses_string_body_as_json():
    """Pipedream sometimes returns the upstream body as a JSON string."""
    tenant_id = uuid4()
    resolver = StaticConnectAccountResolver(
        {(tenant_id, OnboardingConnector.CLOSE): "apn_x"}
    )
    fake = _FakeConnectClient(
        response={"statusCode": 200, "body": '{"id":"oppo_99"}', "headers": {}}
    )
    client = PipedreamConnectCRMFetchClient(
        _settings(), connect_client=fake, resolver=resolver
    )
    result = await client.fetch(
        CRMFetchRequest(
            tenant_id=tenant_id,
            platform=CRMPlatform.CLOSE,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="oppo_99",
        )
    )
    assert result == {"id": "oppo_99"}


def _write_request(tenant_id, rep_id) -> CRMWriteRequest:
    field_update = FieldUpdate(
        platform=CRMPlatform.CLOSE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="oppo_42",
        field_name="status_label",
        current_value="Active",
        proposed_value="Won",
    )
    return CRMWriteRequest(
        tenant_id=tenant_id,
        rep_id=rep_id,
        platform=CRMPlatform.CLOSE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="oppo_42",
        field_updates=[
            ValidatedFieldUpdate(
                update=field_update,
                normalized_value="Won",
            )
        ],
        notes=[
            CRMNote(
                platform=CRMPlatform.CLOSE,
                kind=CRMRecordKind.OPPORTUNITY,
                external_id="oppo_42",
                body="Demo call went well — moving to evaluation.",
            )
        ],
        idempotency_key="test-key-1",
    )


@pytest.mark.asyncio
async def test_write_close_field_updates_then_note():
    tenant_id = uuid4()
    rep_id = uuid4()
    resolver = StaticConnectAccountResolver(
        {(tenant_id, OnboardingConnector.CLOSE): "apn_close_1"}
    )
    fake = _FakeConnectClient(
        response={"statusCode": 200, "body": {"ok": True}, "headers": {}}
    )
    client = PipedreamConnectCRMWriteClient(
        _settings(), connect_client=fake, resolver=resolver
    )

    result = await client.write(_write_request(tenant_id, rep_id))

    assert result.status is CRMWriteStatus.SUCCEEDED
    assert result.succeeded_fields == ["status_label"]
    # First call: PUT to opportunity URL. Second call: POST note.
    assert len(fake.calls) == 2
    put_call, note_call = fake.calls
    assert put_call["method"] == "PUT"
    assert put_call["url"] == "https://api.close.com/api/v1/opportunity/oppo_42/"
    assert put_call["json_body"] == {"status_label": "Won"}
    assert note_call["method"] == "POST"
    assert note_call["url"] == "https://api.close.com/api/v1/activity/note/"
    assert note_call["json_body"]["opportunity_id"] == "oppo_42"


@pytest.mark.asyncio
async def test_write_partial_failure_marks_status_partial():
    tenant_id = uuid4()
    rep_id = uuid4()
    resolver = StaticConnectAccountResolver(
        {(tenant_id, OnboardingConnector.CLOSE): "apn_x"}
    )

    class _FlakyClient(_FakeConnectClient):
        async def proxy_request(self, **kwargs):
            self.calls.append(kwargs)
            # Field PUT succeeds, note POST fails.
            if kwargs["method"] == "POST":
                return {"statusCode": 500, "body": "boom", "headers": {}}
            return {"statusCode": 200, "body": {"ok": True}, "headers": {}}

    fake = _FlakyClient()
    client = PipedreamConnectCRMWriteClient(
        _settings(), connect_client=fake, resolver=resolver
    )

    result = await client.write(_write_request(tenant_id, rep_id))
    # No PARTIAL enum — mixed result surfaces as FAILED with succeeded_fields
    # still recording what did land.
    assert result.status is CRMWriteStatus.FAILED
    assert result.succeeded_fields == ["status_label"]
    assert result.failed_fields and result.failed_fields[0].startswith("note:")
