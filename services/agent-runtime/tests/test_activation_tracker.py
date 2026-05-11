"""Integration tests for ActivationTracker (WO #16)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.config import Settings
from alex_agent_runtime.db import admin_session
from alex_agent_runtime.schemas import (
    ActivationMilestone,
    CalendarLifecycleState,
    DeliveryRequest,
    FirstProactiveOutputType,
    FirstProactiveSelection,
    IngestionComplete,
    IngestionResult,
    MemoryTier,
    MemoryWrite,
    OutputType,
    TaskApproved,
)
from alex_agent_runtime.services.activation_tracker import (
    TOPIC_ACTIVATION_MILESTONE,
    TOPIC_FIRST_PROACTIVE_SELECTED,
    ActivationTracker,
    attach_activation_tracker,
)
from alex_agent_runtime.services.approval_handler import TOPIC_APPROVAL_APPROVED
from alex_agent_runtime.services.delivery_preferences import DeliveryPreferenceRepo
from alex_agent_runtime.services.delivery_tracker import DeliveryTracker
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.memory_store import MemoryStore
from alex_agent_runtime.services.messaging_delivery_client import (
    StubMessagingDeliveryClient,
)
from alex_agent_runtime.services.onboarding_state_repo import OnboardingStateRepo
from alex_agent_runtime.services.output_router import OutputRouter
from alex_agent_runtime.tenant_context import tenant_scope


def _build_world(*, settings: Settings | None = None) -> tuple[
    ActivationTracker,
    StubMessagingDeliveryClient,
    OnboardingStateRepo,
    MemoryStore,
    EventBus,
]:
    settings = settings or Settings(embedding_dim=1536, activation_proactive_window_hours=24)
    bus = EventBus()
    memory_store = MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536), settings=settings
    )
    state_repo = OnboardingStateRepo()
    tracker = DeliveryTracker(escalation_seconds=1800)
    router = OutputRouter(
        delivery_client=StubMessagingDeliveryClient(),
        preferences=DeliveryPreferenceRepo(),
        tracker=tracker,
    )
    # Replace the stub client recorder with our own so tests can inspect.
    recorder = StubMessagingDeliveryClient()
    router._client = recorder  # type: ignore[attr-defined]
    activation = ActivationTracker(
        memory_store=memory_store,
        state_repo=state_repo,
        output_router=router,
        event_bus=bus,
        settings=settings,
    )
    return activation, recorder, state_repo, memory_store, bus


async def _ensure_onboarding(state_repo: OnboardingStateRepo, *, tenant_id: UUID, rep_id: UUID) -> None:
    await state_repo.get_or_create(tenant_id=tenant_id, rep_id=rep_id)


@pytest.mark.asyncio
async def test_ingestion_complete_sends_summary_and_fallback_intro(
    tenant: UUID, rep: UUID
):
    activation, recorder, state_repo, _ms, bus = _build_world()
    await _ensure_onboarding(state_repo, tenant_id=tenant, rep_id=rep)

    seen: list[FirstProactiveSelection] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_FIRST_PROACTIVE_SELECTED, listener)

    event = IngestionComplete(
        tenant_id=tenant,
        rep_id=rep,
        result=IngestionResult(
            tenant_id=tenant,
            rep_id=rep,
            records_processed=12,
            memories_written=8,
            memories_deduplicated=4,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        ),
    )
    await activation.on_ingestion_complete(event)

    titles = [a.title for _c, a in recorder.calls]
    assert any("Initial ingestion complete" in t for t in titles)
    assert any("Welcome aboard" in t for t in titles)

    state = await state_repo.get(tenant_id=tenant, rep_id=rep)
    assert state is not None
    assert state.ingestion_complete_at is not None
    assert state.first_proactive_at is not None
    assert seen and seen[0].output_type is FirstProactiveOutputType.FALLBACK_INTRO


@pytest.mark.asyncio
async def test_selects_meeting_prep_when_external_meeting_upcoming(
    tenant: UUID, rep: UUID
):
    activation, recorder, state_repo, memory_store, _bus = _build_world()
    await _ensure_onboarding(state_repo, tenant_id=tenant, rep_id=rep)

    start = datetime.now(timezone.utc) + timedelta(hours=2)
    end = start + timedelta(minutes=30)
    body = json.dumps(
        {
            "calendar_event_id": "evt-upcoming",
            "provider": "google_calendar",
            "tenant_id": str(tenant),
            "rep_id": str(rep),
            "rep_email": "rep@alex.example",
            "title": "Discovery — Acme",
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "status": "confirmed",
            "attendees": [{"email": "rep@alex.example"}, {"email": "buyer@acme.example"}],
        }
    )
    await memory_store.write_with_status(
        tenant_id=tenant,
        write=MemoryWrite(
            tier=MemoryTier.ORG,
            kind="calendar.event",
            content=body,
            attributes={
                "calendar_event_id": "evt-upcoming",
                "lifecycle_state": "detected",
                "rep_id": str(rep),
                "is_external": True,
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
            },
            source_uri="google_calendar://event/evt-upcoming",
        ),
        index_embeddings=False,
    )

    event = IngestionComplete(
        tenant_id=tenant,
        rep_id=rep,
        result=IngestionResult(
            tenant_id=tenant,
            rep_id=rep,
            records_processed=1,
            memories_written=1,
            memories_deduplicated=0,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        ),
    )
    selection = await activation._select_first_output(event)
    assert selection.output_type is FirstProactiveOutputType.MEETING_PREP
    assert selection.payload["calendar_event_id"] == "evt-upcoming"


@pytest.mark.asyncio
async def test_first_approval_records_milestone_and_publishes(
    tenant: UUID, rep: UUID
):
    activation, _recorder, state_repo, _ms, bus = _build_world()
    await _ensure_onboarding(state_repo, tenant_id=tenant, rep_id=rep)
    # Simulate ingestion completed so the state row exists with proactive flag.
    with tenant_scope(tenant):
        await state_repo.mark_ingestion_complete(tenant_id=tenant, rep_id=rep)

    milestones: list[ActivationMilestone] = []

    async def listener(payload):
        milestones.append(payload)

    bus.subscribe(TOPIC_ACTIVATION_MILESTONE, listener)
    attach_activation_tracker(bus=bus, tracker=activation)

    task_id = uuid4()
    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant,
            rep_id=rep,
            task_id=task_id,
            task_type="email.send",
            payload={"body": "ok"},
        ),
    )
    assert milestones and milestones[0].task_id == task_id

    async with admin_session() as session:
        milestone_ts = await session.scalar(
            text(
                "SELECT activation_milestone_at FROM onboarding_state "
                "WHERE tenant_id = :t AND rep_id = :r"
            ),
            {"t": str(tenant), "r": str(rep)},
        )
    assert milestone_ts is not None


@pytest.mark.asyncio
async def test_subsequent_approvals_do_not_re_record_milestone(
    tenant: UUID, rep: UUID
):
    activation, _recorder, state_repo, _ms, bus = _build_world()
    await _ensure_onboarding(state_repo, tenant_id=tenant, rep_id=rep)
    with tenant_scope(tenant):
        await state_repo.mark_ingestion_complete(tenant_id=tenant, rep_id=rep)
    attach_activation_tracker(bus=bus, tracker=activation)

    milestones: list[ActivationMilestone] = []

    async def listener(payload):
        milestones.append(payload)

    bus.subscribe(TOPIC_ACTIVATION_MILESTONE, listener)

    first_id = uuid4()
    second_id = uuid4()
    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant, rep_id=rep, task_id=first_id, task_type="email.send",
            payload={},
        ),
    )
    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant, rep_id=rep, task_id=second_id, task_type="email.send",
            payload={},
        ),
    )
    # Only the first one published a milestone event.
    assert len(milestones) == 1
    assert milestones[0].task_id == first_id


@pytest.mark.asyncio
async def test_fallback_scan_fires_for_overdue_reps(tenant: UUID, rep: UUID):
    activation, recorder, state_repo, _ms, _bus = _build_world(
        settings=Settings(embedding_dim=1536, activation_proactive_window_hours=24)
    )
    await _ensure_onboarding(state_repo, tenant_id=tenant, rep_id=rep)
    # Force ingestion_complete_at to >24h ago and first_proactive_at = null.
    async with admin_session() as session:
        await session.execute(
            text(
                "UPDATE onboarding_state "
                "SET ingestion_complete_at = :ts, first_proactive_at = NULL "
                "WHERE tenant_id = :t AND rep_id = :r"
            ),
            {
                "ts": datetime.now(timezone.utc) - timedelta(hours=48),
                "t": str(tenant),
                "r": str(rep),
            },
        )

    emitted = await activation.run_fallback_scan()
    assert emitted == 1
    titles = [a.title for _c, a in recorder.calls]
    assert any("Welcome aboard" in t for t in titles)

    state = await state_repo.get(tenant_id=tenant, rep_id=rep)
    assert state is not None and state.first_proactive_at is not None
