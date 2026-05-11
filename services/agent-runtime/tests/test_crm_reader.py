"""Tests for the CRMReader."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    CRMFetchRequest,
    CRMPlatform,
    CRMRecord,
    CRMRecordKind,
    IntegrationEvent,
    MemoryTier,
)
from alex_agent_runtime.services.crm_reader import CRMReader, CRMReaderError
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.memory_store import MemoryStore


def _store() -> MemoryStore:
    return MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(embedding_dim=1536),
    )


class StubFetchClient:
    name = "test-stub"

    def __init__(self) -> None:
        self.calls: list[CRMFetchRequest] = []
        self.returns: dict[tuple[str, str], dict] = {}

    def stage(self, *, platform: CRMPlatform, external_id: str, payload: dict) -> None:
        self.returns[(platform.value, external_id)] = payload

    async def fetch(self, request: CRMFetchRequest):
        self.calls.append(request)
        return self.returns.get((request.platform.value, request.external_id))


def _event_payload(*, tenant_id: UUID, record: dict, subscription_type: str = "deal.propertyChange") -> dict:
    return {
        "tenant_id": str(tenant_id),
        "subscription_type": subscription_type,
        "record": record,
    }


@pytest.mark.asyncio
async def test_handle_data_sync_caches_normalised_record(tenant: UUID, rep: UUID):
    store = _store()
    fetch = StubFetchClient()
    reader = CRMReader(memory_store=store, fetch_client=fetch)
    record_raw = {
        "id": "deal-handle-1",
        "properties": {
            "dealname": "Acme Q4",
            "dealstage": "qualification",
            "amount": "9000",
            "deal_currency_code": "EUR",
        },
    }
    event = IntegrationEvent(
        event_id="evt-1",
        source="hubspot",
        kind="crm.activity_logged",
        occurred_at=datetime.now(timezone.utc),
        payload=_event_payload(tenant_id=tenant, record=record_raw),
    )
    result = await reader.handle_data_sync(event)
    assert result is not None
    assert result.platform is CRMPlatform.HUBSPOT
    assert result.kind is CRMRecordKind.OPPORTUNITY
    assert result.cached is True
    assert result.deduplicated is False

    # Stored in the cache; second call dedupes.
    again = await reader.handle_data_sync(event)
    assert again.deduplicated is True


@pytest.mark.asyncio
async def test_handle_data_sync_rejects_missing_tenant_id():
    reader = CRMReader(memory_store=_store(), fetch_client=StubFetchClient())
    event = IntegrationEvent(
        event_id="evt-x",
        source="hubspot",
        kind="crm.activity_logged",
        occurred_at=datetime.now(timezone.utc),
        payload={"subscription_type": "deal.creation", "record": {"id": "x"}},
    )
    with pytest.raises(CRMReaderError):
        await reader.handle_data_sync(event)


@pytest.mark.asyncio
async def test_handle_data_sync_skips_unsupported_source(tenant: UUID):
    reader = CRMReader(memory_store=_store(), fetch_client=StubFetchClient())
    event = IntegrationEvent(
        event_id="evt-y",
        source="oracle-cx",
        kind="crm.activity_logged",
        occurred_at=datetime.now(timezone.utc),
        payload={"tenant_id": str(tenant), "subscription_type": "deal.creation", "record": {"id": "x"}},
    )
    assert await reader.handle_data_sync(event) is None


@pytest.mark.asyncio
async def test_fetch_record_hits_cache_after_handle(tenant: UUID):
    store = _store()
    fetch = StubFetchClient()
    reader = CRMReader(memory_store=store, fetch_client=fetch)
    record_raw = {
        "id": "deal-cache-1",
        "properties": {"dealname": "Cached Deal"},
    }
    event = IntegrationEvent(
        event_id="evt-cache",
        source="hubspot",
        kind="crm.activity_logged",
        occurred_at=datetime.now(timezone.utc),
        payload=_event_payload(tenant_id=tenant, record=record_raw),
    )
    await reader.handle_data_sync(event)

    fetched = await reader.fetch_record(
        tenant_id=tenant,
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-cache-1",
    )
    assert fetched is not None
    assert fetched.name == "Cached Deal"
    # Cache hit — the fetch client must not have been called.
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_fetch_record_falls_back_to_pipedream(tenant: UUID):
    fetch = StubFetchClient()
    fetch.stage(
        platform=CRMPlatform.SALESFORCE,
        external_id="006xxFETCH",
        payload={
            "Id": "006xxFETCH",
            "Name": "Fetched from Salesforce",
            "StageName": "Proposal",
            "Amount": 12500,
            "CurrencyIsoCode": "USD",
            "Probability": 50,
            "CloseDate": "2026-09-30",
            "Owner": {"Email": "rep@example.com"},
            "AccountId": "001xxFETCH",
        },
    )
    reader = CRMReader(memory_store=_store(), fetch_client=fetch)
    record = await reader.fetch_record(
        tenant_id=tenant,
        platform=CRMPlatform.SALESFORCE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="006xxFETCH",
    )
    assert record is not None
    assert record.name == "Fetched from Salesforce"
    assert record.amount_cents == 1_250_000
    assert record.probability == 0.5
    # And it should now be cached for next time.
    again = await reader.fetch_record(
        tenant_id=tenant,
        platform=CRMPlatform.SALESFORCE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="006xxFETCH",
    )
    assert again is not None
    assert len(fetch.calls) == 1


@pytest.mark.asyncio
async def test_fetch_record_returns_none_on_miss(tenant: UUID):
    reader = CRMReader(memory_store=_store(), fetch_client=StubFetchClient())
    record = await reader.fetch_record(
        tenant_id=tenant,
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="missing-deal",
    )
    assert record is None
