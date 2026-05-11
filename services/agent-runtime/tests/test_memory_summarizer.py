"""Tests for MemorySummarizer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    AgentResponse,
    MemoryTier,
    MemoryWrite,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.memory_store import MemoryStore
from alex_agent_runtime.services.memory_summarizer import (
    MemorySummarizer,
    SummarizerError,
    is_summary_kind,
)


class RecordingAgentBackend:
    """Captures the prompts it sees so tests can assert on them."""

    name = "recorder"

    def __init__(self, text: str = "stub summary") -> None:
        self._text = text
        self.calls: list[dict[str, object]] = []

    async def run(self, prompt: str, *, system_prompt: str | None = None, max_turns: int = 1):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "max_turns": max_turns}
        )
        return AgentResponse(text=self._text, backend=self.name)


def _store(*, share_org: bool = False, staleness: int = 6 * 3600) -> MemoryStore:
    return MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(
            embedding_dim=1536,
            default_share_rep_memories_across_org=share_org,
            summary_staleness_seconds=staleness,
        ),
    )


async def _seed_deal(tenant: UUID, rep: UUID) -> UUID:
    from sqlalchemy import text

    from alex_agent_runtime.db import transactional_session
    from alex_agent_runtime.tenant_context import tenant_scope
    from uuid import uuid4

    account_id = uuid4()
    deal_id = uuid4()
    with tenant_scope(tenant):
        async with transactional_session() as session:
            await session.execute(
                text("INSERT INTO accounts (id, tenant_id, name) VALUES (:id, :t, 'A')"),
                {"id": str(account_id), "t": str(tenant)},
            )
            await session.execute(
                text(
                    "INSERT INTO deals (id, tenant_id, account_id, owner_rep_id, name) "
                    "VALUES (:id, :t, :acct, :rep, 'Acme')"
                ),
                {
                    "id": str(deal_id),
                    "t": str(tenant),
                    "acct": str(account_id),
                    "rep": str(rep),
                },
            )
    return deal_id


@pytest.mark.asyncio
async def test_summarize_for_writes_summary_row(tenant: UUID, rep: UUID):
    store = _store()
    backend = RecordingAgentBackend("compact bullet summary")
    bus = EventBus()
    summarizer = MemorySummarizer(
        memory_store=store, agent_backend=backend, event_bus=bus, settings=store._settings
    )

    deal_id = await _seed_deal(tenant, rep)
    for note in ("note 1 — discovery", "note 2 — pricing question"):
        await store.write(
            tenant_id=tenant,
            write=MemoryWrite(
                tier=MemoryTier.DEAL,
                owner_id=deal_id,
                kind="interaction_note",
                content=note,
            ),
        )

    record = await summarizer.summarize_for(
        tenant_id=tenant, tier=MemoryTier.DEAL, owner_id=deal_id
    )
    assert record is not None
    assert is_summary_kind(record.kind)
    assert record.content == "compact bullet summary"
    assert record.attributes["model_backend"] == "recorder"
    assert len(record.attributes["source_ids"]) == 2
    assert len(backend.calls) == 1, "agent backend should be called exactly once"


@pytest.mark.asyncio
async def test_summarize_for_skips_when_no_sources(tenant: UUID, rep: UUID):
    store = _store()
    summarizer = MemorySummarizer(
        memory_store=store,
        agent_backend=RecordingAgentBackend(),
        event_bus=EventBus(),
        settings=store._settings,
    )
    deal_id = await _seed_deal(tenant, rep)
    assert (
        await summarizer.summarize_for(
            tenant_id=tenant, tier=MemoryTier.DEAL, owner_id=deal_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_summarize_for_is_skipped_when_summary_is_fresh(tenant: UUID, rep: UUID):
    """When a summary already exists and the underlying sources haven't
    moved since, we should re-use it rather than re-prompt the model."""
    store = _store(staleness=24 * 3600)  # 1 day
    backend = RecordingAgentBackend("first summary")
    bus = EventBus()
    summarizer = MemorySummarizer(
        memory_store=store, agent_backend=backend, event_bus=bus, settings=store._settings
    )
    deal_id = await _seed_deal(tenant, rep)
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.DEAL,
            owner_id=deal_id,
            kind="interaction_note",
            content="seed",
        ),
    )
    first = await summarizer.summarize_for(
        tenant_id=tenant, tier=MemoryTier.DEAL, owner_id=deal_id
    )
    assert first is not None
    again = await summarizer.summarize_for(
        tenant_id=tenant, tier=MemoryTier.DEAL, owner_id=deal_id
    )
    assert again is not None
    assert again.id == first.id
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_summarize_for_emits_event(tenant: UUID, rep: UUID):
    store = _store()
    backend = RecordingAgentBackend("x")
    bus = EventBus()
    received: list[object] = []

    async def listener(payload):
        received.append(payload)

    bus.subscribe("memory.summary_updated", listener)

    summarizer = MemorySummarizer(
        memory_store=store, agent_backend=backend, event_bus=bus, settings=store._settings
    )
    deal_id = await _seed_deal(tenant, rep)
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.DEAL,
            owner_id=deal_id,
            kind="interaction_note",
            content="trigger",
        ),
    )
    await summarizer.summarize_for(
        tenant_id=tenant, tier=MemoryTier.DEAL, owner_id=deal_id
    )
    assert len(received) == 1


@pytest.mark.asyncio
async def test_summarize_for_raises_on_empty_response(tenant: UUID, rep: UUID):
    store = _store()
    summarizer = MemorySummarizer(
        memory_store=store,
        agent_backend=RecordingAgentBackend(""),
        event_bus=EventBus(),
        settings=store._settings,
    )
    deal_id = await _seed_deal(tenant, rep)
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.DEAL,
            owner_id=deal_id,
            kind="interaction_note",
            content="x",
        ),
    )
    with pytest.raises(SummarizerError):
        await summarizer.summarize_for(
            tenant_id=tenant, tier=MemoryTier.DEAL, owner_id=deal_id
        )
