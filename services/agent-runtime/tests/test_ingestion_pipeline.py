"""Tests for IngestionProvider + IngestionPipeline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    IngestedRecord,
    IngestedRecordKind,
    IngestionBatch,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.ingestion_pipeline import IngestionPipeline
from alex_agent_runtime.services.ingestion_provider import StubIngestionProvider
from alex_agent_runtime.services.memory_store import MemoryStore


def _pipeline_with_provider(provider, *, recording_cap: int = 5) -> IngestionPipeline:
    store = MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(embedding_dim=1536),
    )
    bus = EventBus()
    pipeline = IngestionPipeline(
        provider=provider,
        memory_store=store,
        event_bus=bus,
        settings=Settings(embedding_dim=1536, ingestion_recording_cap=recording_cap),
    )
    return pipeline


@pytest.mark.asyncio
async def test_stub_provider_returns_canonical_batch():
    provider = StubIngestionProvider()
    tenant = UUID(int=1)
    rep = UUID(int=2)
    batch = await provider.fetch_batch(tenant_id=tenant, rep_id=rep, since_days=30)
    assert isinstance(batch, IngestionBatch)
    assert batch.tenant_id == tenant
    assert batch.rep_id == rep
    kinds = {r.kind for r in batch.records}
    assert IngestedRecordKind.CRM_OPPORTUNITY in kinds
    assert IngestedRecordKind.EMAIL_THREAD in kinds


@pytest.mark.asyncio
async def test_pipeline_runs_against_stub(tenant: UUID, rep: UUID):
    pipeline = _pipeline_with_provider(StubIngestionProvider())
    received: list[object] = []
    pipeline._event_bus.subscribe("ingestion.complete", lambda p: _track(received, p))
    result = await pipeline.run(tenant_id=tenant, rep_id=rep, since_days=30)
    assert result.records_processed == 4
    # Every record routes to ORG tier in the scaffold (no resolver yet),
    # which means 4 writes succeed with 4 distinct contents.
    assert result.memories_written == 4
    assert result.errors == []
    assert len(received) == 1


@pytest.mark.asyncio
async def test_pipeline_is_idempotent_on_replay(tenant: UUID, rep: UUID):
    """Re-running ingestion with the same provider output should not
    write new rows the second time — MemoryStore's content-hash dedup
    handles this end-to-end."""
    pipeline = _pipeline_with_provider(StubIngestionProvider())
    first = await pipeline.run(tenant_id=tenant, rep_id=rep, since_days=30)
    second = await pipeline.run(tenant_id=tenant, rep_id=rep, since_days=30)
    assert first.memories_written == 4
    assert second.memories_written == 0
    assert second.memories_deduplicated == 4


@pytest.mark.asyncio
async def test_pipeline_caps_recordings():
    class ManyRecordingsProvider:
        name = "many-recordings"

        async def fetch_batch(self, *, tenant_id, rep_id, since_days):
            now = datetime.now(timezone.utc)
            recordings = [
                IngestedRecord(
                    kind=IngestedRecordKind.CALL_RECORDING,
                    external_id=f"call-{i}",
                    content=f"recording {i}",
                    occurred_at=now - timedelta(days=i),
                )
                for i in range(10)
            ]
            non_recordings = [
                IngestedRecord(
                    kind=IngestedRecordKind.EMAIL_THREAD,
                    external_id="thread-1",
                    content="email body",
                    occurred_at=now,
                )
            ]
            return IngestionBatch(
                tenant_id=tenant_id,
                rep_id=rep_id,
                fetched_at=now,
                records=non_recordings + recordings,
            )

    pipeline = _pipeline_with_provider(ManyRecordingsProvider(), recording_cap=3)
    batch_before_cap = await ManyRecordingsProvider().fetch_batch(
        tenant_id=UUID(int=1), rep_id=UUID(int=2), since_days=30
    )
    capped = pipeline._cap_recordings(batch_before_cap)
    recordings = [r for r in capped.records if r.kind is IngestedRecordKind.CALL_RECORDING]
    assert len(recordings) == 3
    # Most recent recordings are kept.
    assert [r.external_id for r in recordings] == ["call-0", "call-1", "call-2"]
    # Non-recording records pass through.
    assert any(r.kind is IngestedRecordKind.EMAIL_THREAD for r in capped.records)


@pytest.mark.asyncio
async def test_pipeline_surfaces_provider_failure_in_result(tenant: UUID, rep: UUID):
    class FailingProvider:
        name = "failing"

        async def fetch_batch(self, *, tenant_id, rep_id, since_days):
            raise RuntimeError("provider exploded")

    pipeline = _pipeline_with_provider(FailingProvider())
    result = await pipeline.run(tenant_id=tenant, rep_id=rep, since_days=30)
    assert result.records_processed == 0
    assert result.errors and "provider_fetch_failed" in result.errors[0]


def _track(receiver: list, payload):
    async def _inner():
        receiver.append(payload)
    # Hand back an awaitable so the EventBus can `await` it.
    return _inner()
