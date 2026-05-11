"""Tests for VoiceApplicator (prompt fragment + DACH register)."""
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
from alex_agent_runtime.services.voice_applicator import (
    AccountContext,
    VoiceApplicator,
)
from alex_agent_runtime.services.voice_profile_store import VoiceProfileStore


def _store() -> VoiceProfileStore:
    return VoiceProfileStore(
        memory_store=MemoryStore(
            embedding_client=StubEmbeddingClient(dim=1536),
            settings=Settings(embedding_dim=1536),
        )
    )


def _seeded_profile(*, rep_id: UUID) -> VoiceProfile:
    return VoiceProfile(
        rep_id=rep_id,
        version=4,
        languages={
            VoiceLanguage.EN: VoiceLanguageProfile(
                greetings=[
                    VoicePatternStat(phrase="Hi Sam", weight=0.7),
                    VoicePatternStat(phrase="Hello Sam", weight=0.02),  # below threshold
                ],
                signoffs=[VoicePatternStat(phrase="Cheers", weight=0.5)],
                sample_count=4,
                formality=0.3,
                directness=0.7,
                brevity=0.6,
            ),
            VoiceLanguage.DE: VoiceLanguageProfile(
                greetings=[
                    VoicePatternStat(phrase="Sehr geehrte Frau Müller", weight=0.6),
                ],
                signoffs=[VoicePatternStat(phrase="Mit freundlichen Grüßen", weight=0.8)],
                sample_count=2,
                de_register=GermanRegister.SIE,
                formality=0.8,
            ),
        },
    )


@pytest.mark.asyncio
async def test_applicator_renders_english_voice_with_metadata(tenant: UUID, rep: UUID):
    store = _store()
    await store.put(tenant_id=tenant, rep_id=rep, profile=_seeded_profile(rep_id=rep))
    applicator = VoiceApplicator(store=store)
    result = await applicator.apply(
        tenant_id=tenant,
        rep_id=rep,
        context=AccountContext(account_external_id="acct-1", language=VoiceLanguage.EN),
    )
    assert "Hi Sam" in result.prompt_fragment
    # Below-threshold pattern is suppressed.
    assert "Hello Sam" not in result.prompt_fragment
    assert "Cheers" in result.prompt_fragment
    assert "formality" in result.prompt_fragment.lower()
    assert result.application.profile_version == 4
    assert result.application.language is VoiceLanguage.EN
    # English path doesn't carry a register.
    assert result.application.de_register is None


@pytest.mark.asyncio
async def test_applicator_resolves_sie_register_from_profile(tenant: UUID, rep: UUID):
    store = _store()
    await store.put(tenant_id=tenant, rep_id=rep, profile=_seeded_profile(rep_id=rep))
    applicator = VoiceApplicator(store=store)
    result = await applicator.apply(
        tenant_id=tenant,
        rep_id=rep,
        context=AccountContext(
            account_external_id="acct-de", language=VoiceLanguage.DE, country_hint="DE"
        ),
    )
    assert result.application.de_register is GermanRegister.SIE
    assert "Sie" in result.prompt_fragment or "sie" in result.prompt_fragment
    assert "Mit freundlichen Grüßen" in result.prompt_fragment


@pytest.mark.asyncio
async def test_applicator_account_hint_overrides_profile_register(
    tenant: UUID, rep: UUID
):
    """If the rep already settled on Du for this account, the applicator
    respects it even though the profile-wide default is Sie."""
    store = _store()
    await store.put(tenant_id=tenant, rep_id=rep, profile=_seeded_profile(rep_id=rep))
    applicator = VoiceApplicator(store=store)
    result = await applicator.apply(
        tenant_id=tenant,
        rep_id=rep,
        context=AccountContext(
            language=VoiceLanguage.DE, register_hint=GermanRegister.DU
        ),
    )
    assert result.application.de_register is GermanRegister.DU


@pytest.mark.asyncio
async def test_applicator_returns_neutral_fallback_when_profile_empty(
    tenant: UUID, rep: UUID
):
    applicator = VoiceApplicator(store=_store())
    result = await applicator.apply(
        tenant_id=tenant,
        rep_id=rep,
        context=AccountContext(language=VoiceLanguage.EN),
    )
    # version=0 is the default-profile sentinel.
    assert result.application.profile_version == 0
    assert "empty" in result.prompt_fragment.lower()
