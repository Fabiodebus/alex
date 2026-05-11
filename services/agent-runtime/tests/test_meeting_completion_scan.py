"""Integration tests for MeetingCompletionScan."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventStatus,
    CalendarProvider,
    MeetingCompleted,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.meeting_classifier import MeetingClassifier
from alex_agent_runtime.services.meeting_completion_scan import MeetingCompletionScan
from alex_agent_runtime.services.meeting_events import (
    MeetingEventEmitter,
    TOPIC_MEETING_COMPLETED,
)
from alex_agent_runtime.services.memory_store import MemoryStore


def _store() -> MemoryStore:
    return MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(embedding_dim=1536),
    )


def _past_event(*, tenant_id: UUID, rep_id: UUID, calendar_event_id: str) -> CalendarEvent:
    """Past meeting whose end_at is well behind the grace window."""
    end = datetime.now(timezone.utc) - timedelta(minutes=15)
    start = end - timedelta(minutes=30)
    return CalendarEvent(
        provider=CalendarProvider.GOOGLE,
        calendar_event_id=calendar_event_id,
        tenant_id=tenant_id,
        rep_id=rep_id,
        rep_email="rep@alex.example",
        title="Demo Call",
        start_at=start,
        end_at=end,
        status=CalendarEventStatus.CONFIRMED,
        attendees=[
            CalendarAttendee(email="rep@alex.example", is_organizer=True),
            CalendarAttendee(email="buyer@acme.example"),
        ],
    )


def _future_event(*, tenant_id: UUID, rep_id: UUID, calendar_event_id: str) -> CalendarEvent:
    start = datetime.now(timezone.utc) + timedelta(minutes=60)
    end = start + timedelta(minutes=30)
    return CalendarEvent(
        provider=CalendarProvider.GOOGLE,
        calendar_event_id=calendar_event_id,
        tenant_id=tenant_id,
        rep_id=rep_id,
        rep_email="rep@alex.example",
        title="Upcoming",
        start_at=start,
        end_at=end,
        attendees=[
            CalendarAttendee(email="rep@alex.example", is_organizer=True),
            CalendarAttendee(email="buyer@acme.example"),
        ],
    )


@pytest.mark.asyncio
async def test_scan_emits_completed_for_past_detected_meeting(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    emitter = MeetingEventEmitter(bus)
    classifier = MeetingClassifier(memory_store=store, emitter=emitter)
    scan = MeetingCompletionScan(memory_store=store, emitter=emitter)

    seen: list[MeetingCompleted] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_COMPLETED, listener)

    await classifier.classify(_past_event(tenant_id=tenant, rep_id=rep, calendar_event_id="evt-past"))
    await classifier.classify(_future_event(tenant_id=tenant, rep_id=rep, calendar_event_id="evt-future"))

    emitted = await scan.run_once()
    assert emitted == 1
    assert seen and seen[0].calendar_event_id == "evt-past"


@pytest.mark.asyncio
async def test_scan_does_not_double_emit(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    emitter = MeetingEventEmitter(bus)
    classifier = MeetingClassifier(memory_store=store, emitter=emitter)
    scan = MeetingCompletionScan(memory_store=store, emitter=emitter)

    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_COMPLETED, listener)

    await classifier.classify(_past_event(tenant_id=tenant, rep_id=rep, calendar_event_id="evt-once"))
    first = await scan.run_once()
    second = await scan.run_once()
    assert first == 1
    assert second == 0
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_scan_skips_cancelled_meeting(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    emitter = MeetingEventEmitter(bus)
    classifier = MeetingClassifier(memory_store=store, emitter=emitter)
    scan = MeetingCompletionScan(memory_store=store, emitter=emitter)

    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_COMPLETED, listener)

    event = _past_event(tenant_id=tenant, rep_id=rep, calendar_event_id="evt-cancel-then-scan")
    await classifier.classify(event)
    await classifier.classify(event.model_copy(update={"status": CalendarEventStatus.CANCELLED}))

    emitted = await scan.run_once()
    assert emitted == 0
    assert seen == []


@pytest.mark.asyncio
async def test_scan_skips_events_inside_grace_window(tenant: UUID, rep: UUID):
    """A meeting that ended 1 minute ago shouldn't fire (grace=5 min)."""
    store = _store()
    bus = EventBus()
    emitter = MeetingEventEmitter(bus)
    classifier = MeetingClassifier(memory_store=store, emitter=emitter)
    scan = MeetingCompletionScan(memory_store=store, emitter=emitter)

    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_COMPLETED, listener)

    just_ended = datetime.now(timezone.utc) - timedelta(seconds=30)
    event = CalendarEvent(
        provider=CalendarProvider.GOOGLE,
        calendar_event_id="evt-just-ended",
        tenant_id=tenant,
        rep_id=rep,
        rep_email="rep@alex.example",
        title="Just Ended",
        start_at=just_ended - timedelta(minutes=30),
        end_at=just_ended,
        attendees=[
            CalendarAttendee(email="rep@alex.example", is_organizer=True),
            CalendarAttendee(email="buyer@acme.example"),
        ],
    )
    await classifier.classify(event)
    emitted = await scan.run_once()
    assert emitted == 0
    assert seen == []
