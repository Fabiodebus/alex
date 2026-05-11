"""Live-DB tests for MemoryStore."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.config import Settings
from alex_agent_runtime.db import admin_session, transactional_session
from alex_agent_runtime.schemas import (
    MemoryContext,
    MemoryTier,
    MemoryWrite,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.memory_store import MemoryStore
from alex_agent_runtime.tenant_context import tenant_scope


def _store(*, share_org: bool = False) -> MemoryStore:
    return MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(
            embedding_dim=1536,
            default_share_rep_memories_across_org=share_org,
        ),
    )


async def _seed_deal(tenant: UUID, rep: UUID) -> tuple[UUID, UUID]:
    """Returns (account_id, deal_id) for tests that need deal/account-tier writes."""
    account_id = uuid4()
    deal_id = uuid4()
    with tenant_scope(tenant):
        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO accounts (id, tenant_id, name) "
                    "VALUES (:id, :tenant_id, 'Acme')"
                ),
                {"id": str(account_id), "tenant_id": str(tenant)},
            )
            await session.execute(
                text(
                    "INSERT INTO deals (id, tenant_id, account_id, owner_rep_id, name) "
                    "VALUES (:id, :tenant_id, :acct, :rep, 'Acme Q3')"
                ),
                {
                    "id": str(deal_id),
                    "tenant_id": str(tenant),
                    "acct": str(account_id),
                    "rep": str(rep),
                },
            )
    return account_id, deal_id


@pytest.mark.asyncio
async def test_write_then_retrieve_returns_record(tenant: UUID, rep: UUID):
    store = _store()
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP,
            owner_id=rep,
            kind="voice_sample",
            content="Alice writes in a friendly, concise style.",
        ),
    )
    summary = await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=rep,
            tiers=[MemoryTier.REP],
            query_text=None,
        )
    )
    snippets = summary.by_tier[MemoryTier.REP]
    assert len(snippets) == 1
    assert "friendly" in snippets[0].memory.content


@pytest.mark.asyncio
async def test_write_dedupes_on_content_hash(tenant: UUID, rep: UUID):
    store = _store()
    payload = MemoryWrite(
        tier=MemoryTier.REP,
        owner_id=rep,
        kind="voice_sample",
        content="duplicate content sample",
    )
    first = await store.write(tenant_id=tenant, write=payload)
    second = await store.write(tenant_id=tenant, write=payload)
    assert first.id == second.id

    # And only one row + one embedding chunk should exist.
    with tenant_scope(tenant):
        async with transactional_session() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM rep_memories WHERE rep_id = :r"),
                {"r": str(rep)},
            )
            chunks = await session.scalar(
                text(
                    "SELECT count(*) FROM rep_memory_embeddings WHERE source_id = :id"
                ),
                {"id": str(first.id)},
            )
    assert count == 1
    assert chunks >= 1


@pytest.mark.asyncio
async def test_retrieve_with_query_uses_ann(tenant: UUID, rep: UUID):
    store = _store()
    # Three rep memories — the third is semantically closest to the query.
    contents = [
        "Alice likes long-form, narrative emails.",
        "Alice prefers French Press coffee.",
        "Alice writes terse one-line follow-ups when the deal is mid-stage.",
    ]
    for content in contents:
        await store.write(
            tenant_id=tenant,
            write=MemoryWrite(
                tier=MemoryTier.REP,
                owner_id=rep,
                kind="voice_sample",
                content=content,
            ),
        )
    summary = await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=rep,
            tiers=[MemoryTier.REP],
            query_text="Alice writes terse one-line follow-ups when the deal is mid-stage.",
            k_per_tier=1,
        )
    )
    top = summary.by_tier[MemoryTier.REP][0]
    assert top.memory.content == contents[2]
    assert top.similarity is not None
    assert top.similarity > 0.99  # exact match via deterministic stub


@pytest.mark.asyncio
async def test_retrieve_deal_tier(tenant: UUID, rep: UUID):
    store = _store()
    _, deal_id = await _seed_deal(tenant, rep)
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.DEAL,
            owner_id=deal_id,
            kind="interaction_note",
            content="Customer asked about pricing tier 2.",
        ),
    )
    summary = await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=rep,
            deal_id=deal_id,
            tiers=[MemoryTier.DEAL],
        )
    )
    snippets = summary.by_tier[MemoryTier.DEAL]
    assert len(snippets) == 1
    assert "tier 2" in snippets[0].memory.content


@pytest.mark.asyncio
async def test_org_tier_write_and_retrieve(tenant: UUID, rep: UUID):
    store = _store()
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.ORG,
            owner_id=None,
            kind="playbook",
            content="ICP: DACH B2B Series B+, ACV €10k+, 30+ day cycles.",
        ),
    )
    summary = await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=rep,
            tiers=[MemoryTier.ORG],
        )
    )
    snippets = summary.by_tier[MemoryTier.ORG]
    assert len(snippets) == 1
    assert "ICP" in snippets[0].memory.content


@pytest.mark.asyncio
async def test_org_share_flag_controls_cross_rep_visibility(tenant: UUID, rep: UUID):
    """When sharing is off, retrieve scoped to a DIFFERENT rep doesn't see this rep's memory."""
    store = _store(share_org=False)
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP,
            owner_id=rep,
            kind="voice_sample",
            content="rep-private voice sample",
        ),
    )

    # Make a second rep on the same tenant.
    other_rep = uuid4()
    with tenant_scope(tenant):
        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO reps (id, tenant_id, email, display_name) "
                    "VALUES (:id, :tenant_id, :email, 'Other')"
                ),
                {
                    "id": str(other_rep),
                    "tenant_id": str(tenant),
                    "email": f"other-{other_rep}@example.com",
                },
            )

    summary = await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=other_rep,
            tiers=[MemoryTier.REP],
        )
    )
    assert summary.by_tier[MemoryTier.REP] == []

    # Flip the flag in tenant_config and retry — now visible.
    with tenant_scope(tenant):
        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_config (tenant_id, key, value) "
                    "VALUES (current_setting('app.tenant_id')::uuid, "
                    "'org_share_rep_memories', '{\"enabled\": true}'::jsonb)"
                )
            )
    summary2 = await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=other_rep,
            tiers=[MemoryTier.REP],
        )
    )
    contents = [s.memory.content for s in summary2.by_tier[MemoryTier.REP]]
    assert "rep-private voice sample" in contents


@pytest.mark.asyncio
async def test_right_to_deletion_cascades_via_data_layer(tenant: UUID, rep: UUID):
    """Deleting a rep clears their memories AND embeddings in one txn — verified
    via the data-layer FK CASCADE we relied on in WO #1."""
    store = _store()
    record = await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP,
            owner_id=rep,
            kind="voice_sample",
            content="will-be-deleted",
        ),
    )
    with tenant_scope(tenant):
        async with transactional_session() as session:
            mem_before = await session.scalar(
                text("SELECT count(*) FROM rep_memories WHERE id = :id"),
                {"id": str(record.id)},
            )
            emb_before = await session.scalar(
                text("SELECT count(*) FROM rep_memory_embeddings WHERE source_id = :id"),
                {"id": str(record.id)},
            )
    assert mem_before == 1
    assert emb_before >= 1

    # Use admin_session (bypasses RLS) to delete the rep — production GDPR
    # purges run with elevated credentials too.
    async with admin_session() as session:
        await session.execute(
            text("DELETE FROM reps WHERE id = :id"),
            {"id": str(rep)},
        )

    with tenant_scope(tenant):
        async with transactional_session() as session:
            mem_after = await session.scalar(
                text("SELECT count(*) FROM rep_memories WHERE id = :id"),
                {"id": str(record.id)},
            )
            emb_after = await session.scalar(
                text("SELECT count(*) FROM rep_memory_embeddings WHERE source_id = :id"),
                {"id": str(record.id)},
            )
    assert mem_after == 0
    assert emb_after == 0
