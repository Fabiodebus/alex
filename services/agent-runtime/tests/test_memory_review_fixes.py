"""Regression tests for the WO #7 review findings (commit cd20c21).

Each test maps to a specific issue from the Codex review so the failure
output names the contract that's broken if someone reintroduces the
underlying bug.
"""
from __future__ import annotations

import math
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.config import Settings
from alex_agent_runtime.db import transactional_session
from alex_agent_runtime.schemas import (
    MemoryContext,
    MemoryTier,
    MemoryWrite,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.embedding_indexer import _vector_literal
from alex_agent_runtime.services.memory_store import (
    MemoryStore,
    MemoryStoreError,
    _content_hash,
    _is_truthy_share_value,
)
from alex_agent_runtime.tenant_context import tenant_scope


def _store(*, share_org: bool = False) -> MemoryStore:
    return MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(
            embedding_dim=1536,
            default_share_rep_memories_across_org=share_org,
        ),
    )


# ---------------------------------------------------------------------------
# HIGH 1: caller-supplied content_hash must NOT override the computed hash.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_caller_supplied_content_hash_is_overwritten(tenant: UUID, rep: UUID):
    store = _store()
    a = await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP,
            owner_id=rep,
            kind="voice_sample",
            content="payload-A",
            attributes={"content_hash": "ATTACKER_VALUE"},
        ),
    )
    b = await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP,
            owner_id=rep,
            kind="voice_sample",
            content="payload-B",
            attributes={"content_hash": "ATTACKER_VALUE"},
        ),
    )
    assert a.id != b.id, "writes must dedupe on the real content, not on caller attributes"
    assert a.attributes["content_hash"] != "ATTACKER_VALUE"
    assert a.attributes["content_hash"] == _content_hash("voice_sample", "payload-A")
    assert b.attributes["content_hash"] == _content_hash("voice_sample", "payload-B")


# ---------------------------------------------------------------------------
# HIGH 4: dedup key must include `kind`.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dedup_key_includes_kind(tenant: UUID, rep: UUID):
    store = _store()
    sample = "the same physical text body"
    a = await store.write(
        tenant_id=tenant,
        write=MemoryWrite(tier=MemoryTier.REP, owner_id=rep, kind="voice_sample", content=sample),
    )
    b = await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP, owner_id=rep, kind="interaction_note", content=sample
        ),
    )
    assert a.id != b.id, "same content under different kinds must produce two rows"
    # kinds_filter must surface both kinds independently.
    by_voice = await store.list_recent(
        tenant_id=tenant, tier=MemoryTier.REP, owner_id=rep, kinds_filter=["voice_sample"]
    )
    by_note = await store.list_recent(
        tenant_id=tenant, tier=MemoryTier.REP, owner_id=rep, kinds_filter=["interaction_note"]
    )
    assert [m.id for m in by_voice] == [a.id]
    assert [m.id for m in by_note] == [b.id]


# ---------------------------------------------------------------------------
# HIGH 2: dedup race — concurrent identical writes should not produce
# duplicate rows. The unique partial index from migration 0005 backs the
# ON CONFLICT DO NOTHING in MemoryStore.write.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_writes_dedup_via_unique_index(tenant: UUID, rep: UUID):
    """Hammer the same write from two coroutines simultaneously and assert
    the table ends up with exactly one row."""
    import asyncio

    store = _store()
    payload = MemoryWrite(
        tier=MemoryTier.REP,
        owner_id=rep,
        kind="voice_sample",
        content="concurrent-dedupe-test",
    )
    results = await asyncio.gather(
        store.write(tenant_id=tenant, write=payload),
        store.write(tenant_id=tenant, write=payload),
        store.write(tenant_id=tenant, write=payload),
        store.write(tenant_id=tenant, write=payload),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results), results
    ids = {r.id for r in results}  # type: ignore[union-attr]
    assert len(ids) == 1

    with tenant_scope(tenant):
        async with transactional_session() as session:
            count = await session.scalar(
                text(
                    "SELECT count(*) FROM rep_memories "
                    "WHERE rep_id = :r AND deleted_at IS NULL"
                ),
                {"r": str(rep)},
            )
    assert count == 1


# ---------------------------------------------------------------------------
# HIGH 3: robust truthy parsing for org_share_rep_memories.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        # truthy
        (True, True),
        ({"enabled": True}, True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("on", True),
        ("1", True),
        (1, True),
        ({"enabled": "true"}, True),
        # falsy — these are the dangerous ones
        (False, False),
        (None, False),
        ({"enabled": False}, False),
        ("false", False),  # would be True under bool("false")
        ("False", False),
        ("0", False),
        (0, False),
        ({"enabled": "false"}, False),  # would be True under bool("false")
        ("nonsense", False),
    ],
)
def test_is_truthy_share_value(value, expected):
    assert _is_truthy_share_value(value) is expected


@pytest.mark.asyncio
async def test_org_share_falsy_string_does_not_enable(tenant: UUID, rep: UUID):
    store = _store()
    with tenant_scope(tenant):
        async with transactional_session() as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_config (tenant_id, key, value) "
                    "VALUES (current_setting('app.tenant_id')::uuid, "
                    "'org_share_rep_memories', '\"false\"'::jsonb)"
                )
            )
    # Write a memory for rep, then try to read with a different rep — must
    # NOT be visible (sharing is effectively off despite the string "false").
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP,
            owner_id=rep,
            kind="voice_sample",
            content="should-stay-private",
        ),
    )
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
                    "email": f"falsy-{other_rep}@example.com",
                },
            )
    summary = await store.retrieve(
        MemoryContext(tenant_id=tenant, rep_id=other_rep, tiers=[MemoryTier.REP])
    )
    assert summary.by_tier[MemoryTier.REP] == []


# ---------------------------------------------------------------------------
# MED 2: list_recent rejects narrower tiers without owner_id.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_recent_rejects_rep_without_owner_id(tenant: UUID):
    store = _store()
    for tier in (MemoryTier.REP, MemoryTier.DEAL, MemoryTier.ACCOUNT):
        with pytest.raises(MemoryStoreError):
            await store.list_recent(tenant_id=tenant, tier=tier, owner_id=None)


@pytest.mark.asyncio
async def test_list_recent_allows_org_tier_without_owner_id(tenant: UUID):
    store = _store()
    rows = await store.list_recent(tenant_id=tenant, tier=MemoryTier.ORG, owner_id=None)
    assert rows == []  # nothing seeded; the call itself should succeed.


# ---------------------------------------------------------------------------
# MED 3: vector literal rejects non-finite floats up-front.
# ---------------------------------------------------------------------------
def test_vector_literal_rejects_nan():
    with pytest.raises(ValueError):
        _vector_literal([0.1, math.nan, 0.3])


def test_vector_literal_rejects_inf():
    with pytest.raises(ValueError):
        _vector_literal([0.1, math.inf, 0.3])
    with pytest.raises(ValueError):
        _vector_literal([0.1, -math.inf, 0.3])


def test_vector_literal_accepts_finite_floats():
    out = _vector_literal([0.1, 0.0, -0.3])
    assert out.startswith("[") and out.endswith("]")


# ---------------------------------------------------------------------------
# MED 4: retrieve computes the query embedding once across tiers.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retrieve_embeds_query_only_once(tenant: UUID, rep: UUID):
    class CountingEmbedder:
        name = "counting"
        dim = 1536

        def __init__(self) -> None:
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[0.0] * self.dim for _ in texts]

    embedder = CountingEmbedder()
    store = MemoryStore(
        embedding_client=embedder,
        settings=Settings(embedding_dim=1536),
    )
    # Seed a memory so there is something to score against — counts the
    # writes too but we only care about retrieve() afterwards.
    await store.write(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.REP, owner_id=rep, kind="voice_sample", content="anything"
        ),
        index_embeddings=False,
    )
    embedder.calls = 0
    await store.retrieve(
        MemoryContext(
            tenant_id=tenant,
            rep_id=rep,
            tiers=[MemoryTier.REP, MemoryTier.ORG],
            query_text="anything",
        )
    )
    assert embedder.calls == 1, "retrieve must call embed() at most once for the query"
