"""Integration tests for VoiceProfileStore (REP-tier memory CRUD)."""
from __future__ import annotations

from uuid import UUID

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    GermanRegister,
    VoiceLanguage,
    VoiceLanguageProfile,
    VoicePatternStat,
    VoiceProfile,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.memory_store import MemoryStore
from alex_agent_runtime.services.voice_profile_store import (
    VoiceProfileStore,
    VoiceProfileStoreError,
)


def _store() -> VoiceProfileStore:
    return VoiceProfileStore(
        memory_store=MemoryStore(
            embedding_client=StubEmbeddingClient(dim=1536),
            settings=Settings(embedding_dim=1536),
        )
    )


def _profile(*, rep_id: UUID, version: int, greeting: str = "Hi") -> VoiceProfile:
    return VoiceProfile(
        rep_id=rep_id,
        version=version,
        languages={
            VoiceLanguage.EN: VoiceLanguageProfile(
                greetings=[VoicePatternStat(phrase=greeting, weight=0.4)],
                sample_count=version,
            ),
            VoiceLanguage.DE: VoiceLanguageProfile(de_register=GermanRegister.SIE),
        },
    )


@pytest.mark.asyncio
async def test_get_current_returns_default_for_unknown_rep(tenant: UUID, rep: UUID):
    store = _store()
    profile = await store.get_current(tenant_id=tenant, rep_id=rep)
    assert profile.rep_id == rep
    assert profile.version == 0
    assert VoiceLanguage.EN in profile.languages
    assert VoiceLanguage.DE in profile.languages


@pytest.mark.asyncio
async def test_put_then_get_round_trips(tenant: UUID, rep: UUID):
    store = _store()
    written = await store.put(
        tenant_id=tenant, rep_id=rep, profile=_profile(rep_id=rep, version=1)
    )
    assert written.version == 1
    fetched = await store.get_current(tenant_id=tenant, rep_id=rep)
    assert fetched.version == 1
    en = fetched.languages[VoiceLanguage.EN]
    assert any(p.phrase == "Hi" for p in en.greetings)


@pytest.mark.asyncio
async def test_put_rejects_non_monotonic_version(tenant: UUID, rep: UUID):
    store = _store()
    await store.put(tenant_id=tenant, rep_id=rep, profile=_profile(rep_id=rep, version=1))
    with pytest.raises(VoiceProfileStoreError):
        await store.put(
            tenant_id=tenant, rep_id=rep, profile=_profile(rep_id=rep, version=1)
        )


@pytest.mark.asyncio
async def test_list_versions_returns_history_descending(tenant: UUID, rep: UUID):
    store = _store()
    for v in (1, 2, 3):
        await store.put(
            tenant_id=tenant,
            rep_id=rep,
            profile=_profile(rep_id=rep, version=v, greeting=f"v{v}-greet"),
        )
    history = await store.list_versions(tenant_id=tenant, rep_id=rep, limit=5)
    assert [p.version for p in history] == [3, 2, 1]


@pytest.mark.asyncio
async def test_revert_to_writes_new_row_with_bumped_version(tenant: UUID, rep: UUID):
    store = _store()
    await store.put(
        tenant_id=tenant,
        rep_id=rep,
        profile=_profile(rep_id=rep, version=1, greeting="v1-greet"),
    )
    await store.put(
        tenant_id=tenant,
        rep_id=rep,
        profile=_profile(rep_id=rep, version=2, greeting="v2-greet"),
    )
    reverted = await store.revert_to(
        tenant_id=tenant, rep_id=rep, version=1, notes="testing"
    )
    # New version is 3 (one above the current latest, 2), payload from v1.
    assert reverted.version == 3
    en = reverted.languages[VoiceLanguage.EN]
    assert any(p.phrase == "v1-greet" for p in en.greetings)


@pytest.mark.asyncio
async def test_revert_to_unknown_version_raises(tenant: UUID, rep: UUID):
    store = _store()
    await store.put(tenant_id=tenant, rep_id=rep, profile=_profile(rep_id=rep, version=1))
    with pytest.raises(VoiceProfileStoreError):
        await store.revert_to(tenant_id=tenant, rep_id=rep, version=42)
