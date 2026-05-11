"""Integration tests for VoiceUpdater (EWMA + EventBus subscriptions)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    DiscardSignal,
    EditDiff,
    GermanRegister,
    TaskApproved,
    TaskDiscarded,
    VoiceLanguage,
)
from alex_agent_runtime.services.approval_handler import (
    TOPIC_APPROVAL_APPROVED,
    TOPIC_APPROVAL_DISCARDED,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.memory_store import MemoryStore
from alex_agent_runtime.services.voice_profile_store import VoiceProfileStore
from alex_agent_runtime.services.voice_signal_extractor import VoiceSignalExtractor
from alex_agent_runtime.services.voice_updater import VoiceUpdater, attach_updater


def _build_updater(settings: Settings | None = None) -> tuple[
    VoiceUpdater, VoiceProfileStore
]:
    store = VoiceProfileStore(
        memory_store=MemoryStore(
            embedding_client=StubEmbeddingClient(dim=1536),
            settings=settings or Settings(embedding_dim=1536),
        )
    )
    updater = VoiceUpdater(
        store=store,
        extractor=VoiceSignalExtractor(),
        settings=settings,
    )
    return updater, store


def _edit_diff(*, tenant_id: UUID, rep_id: UUID, before: str, after: str) -> EditDiff:
    return EditDiff(
        tenant_id=tenant_id,
        rep_id=rep_id,
        task_id=uuid4(),
        task_type="email.send",
        before={"body": before},
        after={"body": after},
    )


@pytest.mark.asyncio
async def test_first_edit_creates_profile_v1(tenant: UUID, rep: UUID):
    updater, store = _build_updater()
    diff = _edit_diff(
        tenant_id=tenant,
        rep_id=rep,
        before="Hello Sam,\n\nQuick check-in.\n\nBest regards",
        after="Hi Sam,\n\nQuick check-in.\n\nCheers",
    )
    profile = await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=diff)
    assert profile.version == 1
    en = profile.languages[VoiceLanguage.EN]
    greetings = {p.phrase: p.weight for p in en.greetings}
    signoffs = {p.phrase: p.weight for p in en.signoffs}
    assert "Hi Sam" in greetings and greetings["Hi Sam"] > 0
    assert "Cheers" in signoffs and signoffs["Cheers"] > 0
    assert en.sample_count == 1


@pytest.mark.asyncio
async def test_recurring_greeting_stays_high_across_edits(tenant: UUID, rep: UUID):
    """The blueprint's progressive-weighting rule: a recurring pattern
    keeps its weight as new edits arrive; one-offs decay."""
    updater, _store = _build_updater()
    diff = _edit_diff(
        tenant_id=tenant,
        rep_id=rep,
        before="Hello Sam,\n\nx\n\nBest regards",
        after="Hi Sam,\n\nx\n\nCheers",
    )
    first = await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=diff)
    await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=diff)
    third = await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=diff)
    en1 = {p.phrase: p.weight for p in first.languages[VoiceLanguage.EN].greetings}
    en3 = {p.phrase: p.weight for p in third.languages[VoiceLanguage.EN].greetings}
    # The first edit saturates a fresh profile at alpha=1.0; subsequent
    # identical edits stay at or near 1.0 because (1-a)*1 + a*1 = 1.
    assert en1["Hi Sam"] >= 0.5
    assert en3["Hi Sam"] >= en1["Hi Sam"] - 0.05
    assert third.version == 3


@pytest.mark.asyncio
async def test_one_off_edit_does_not_dominate_profile(tenant: UUID, rep: UUID):
    """ADR-001: one outlier edit should not corrupt the profile."""
    updater, _store = _build_updater()
    common = _edit_diff(
        tenant_id=tenant,
        rep_id=rep,
        before="Hello Sam,\n\nx\n\nBest regards",
        after="Hi Sam,\n\nx\n\nCheers",
    )
    outlier = _edit_diff(
        tenant_id=tenant,
        rep_id=rep,
        before="Hi Sam,\n\nx\n\nCheers",
        after="WHATSUP Sam,\n\nx\n\nlater dude",
    )
    # Three "common" edits, one outlier afterwards.
    for _ in range(3):
        await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=common)
    profile = await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=outlier)
    greetings = {p.phrase: p.weight for p in profile.languages[VoiceLanguage.EN].greetings}
    # "Hi Sam" should still outweigh the outlier "WHATSUP Sam".
    assert greetings.get("Hi Sam", 0.0) > greetings.get("WHATSUP Sam", 0.0)


@pytest.mark.asyncio
async def test_german_register_signal_settles_to_sie(tenant: UUID, rep: UUID):
    updater, _store = _build_updater()
    diff = _edit_diff(
        tenant_id=tenant,
        rep_id=rep,
        before="Hallo Sam,\n\ndanke für deine Zeit. Ich melde mich bei dir.\n\nGrüße",
        after=(
            "Sehr geehrter Herr Müller,\n\n"
            "vielen Dank für unser Gespräch. Ich melde mich bei Ihnen mit den Unterlagen.\n\n"
            "Mit freundlichen Grüßen"
        ),
    )
    profile = await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=diff)
    de = profile.languages[VoiceLanguage.DE]
    assert de.de_register is GermanRegister.SIE


@pytest.mark.asyncio
async def test_discard_signal_drops_pattern_weight(tenant: UUID, rep: UUID):
    """A discard counts as a negative signal but doesn't reset the profile."""
    updater, _store = _build_updater()
    # First, accept "Hi Sam" three times so it has real weight.
    diff = _edit_diff(
        tenant_id=tenant,
        rep_id=rep,
        before="Hello Sam,\n\nx\n\nBest regards",
        after="Hi Sam,\n\nx\n\nCheers",
    )
    for _ in range(3):
        await updater.apply_edit_diff(tenant_id=tenant, rep_id=rep, diff=diff)
    before_discard = await updater.apply_edit_diff(
        tenant_id=tenant, rep_id=rep, diff=diff
    )

    # Now discard a draft that uses "Hi Sam"/"Cheers".
    discard = DiscardSignal(
        tenant_id=tenant,
        rep_id=rep,
        task_id=uuid4(),
        discarded_body="Hi Sam,\n\nx\n\nCheers",
    )
    after_discard = await updater.apply_discard(
        tenant_id=tenant, rep_id=rep, signal=discard
    )

    # Profile survived; "Hi Sam" lost some weight.
    before_w = next(
        p.weight for p in before_discard.languages[VoiceLanguage.EN].greetings
        if p.phrase == "Hi Sam"
    )
    after_w = next(
        p.weight for p in after_discard.languages[VoiceLanguage.EN].greetings
        if p.phrase == "Hi Sam"
    )
    assert after_w < before_w
    # And forbidden_phrases gained signal for the discarded body lines.
    forbidden_phrases = {
        p.phrase for p in after_discard.languages[VoiceLanguage.EN].forbidden_phrases
    }
    # Mid-body line 'x' is recorded as forbidden.
    assert "x" in forbidden_phrases or any("x" in fp for fp in forbidden_phrases)


@pytest.mark.asyncio
async def test_approval_approved_subscription_consumes_edit_diff(tenant: UUID, rep: UUID):
    updater, store = _build_updater()
    bus = EventBus()
    attach_updater(bus=bus, updater=updater)

    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant,
            rep_id=rep,
            task_id=uuid4(),
            task_type="email.send",
            payload={"body": "Hi Sam,\n\nx"},
            edit_diff=EditDiff(
                tenant_id=tenant,
                rep_id=rep,
                task_id=uuid4(),
                task_type="email.send",
                before={"body": "Hello Sam,\n\nx\n\nBest regards"},
                after={"body": "Hi Sam,\n\nx\n\nCheers"},
            ),
        ),
    )
    profile = await store.get_current(tenant_id=tenant, rep_id=rep)
    assert profile.version >= 1
    greetings = {p.phrase for p in profile.languages[VoiceLanguage.EN].greetings}
    assert "Hi Sam" in greetings


@pytest.mark.asyncio
async def test_clean_approve_does_not_update_profile(tenant: UUID, rep: UUID):
    """A clean approve (no edit_diff) must not bump the version."""
    updater, store = _build_updater()
    bus = EventBus()
    attach_updater(bus=bus, updater=updater)

    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant,
            rep_id=rep,
            task_id=uuid4(),
            task_type="email.send",
            payload={"body": "anything"},
            edit_diff=None,
        ),
    )
    profile = await store.get_current(tenant_id=tenant, rep_id=rep)
    assert profile.version == 0  # default — no rows written


@pytest.mark.asyncio
async def test_approval_discarded_subscription_handles_event(tenant: UUID, rep: UUID):
    updater, store = _build_updater()
    bus = EventBus()
    attach_updater(bus=bus, updater=updater)

    await bus.publish(
        TOPIC_APPROVAL_DISCARDED,
        TaskDiscarded(
            tenant_id=tenant,
            rep_id=rep,
            task_id=uuid4(),
            task_type="email.send",
            feedback="Hi Sam,\n\nThis draft is too pushy\n\nCheers",
        ),
    )
    profile = await store.get_current(tenant_id=tenant, rep_id=rep)
    # Discard should have produced a row (forbidden_phrases populated).
    assert profile.version >= 1
    forbidden = {p.phrase for p in profile.languages[VoiceLanguage.EN].forbidden_phrases}
    assert any("pushy" in p for p in forbidden) or any("Hi Sam" in p for p in forbidden)
