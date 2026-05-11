"""Integration tests for MeetingClassifier (hits Postgres for memory rows)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventStatus,
    CalendarLifecycleState,
    CalendarProvider,
    CRMPlatform,
    CRMRecord,
    CRMRecordKind,
    MeetingCancelled,
    MeetingDetected,
    MemoryTier,
    MemoryWrite,
)
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.meeting_classifier import MeetingClassifier
from alex_agent_runtime.services.meeting_events import (
    MeetingEventEmitter,
    TOPIC_MEETING_CANCELLED,
    TOPIC_MEETING_DETECTED,
)
from alex_agent_runtime.services.memory_store import MemoryStore


def _store() -> MemoryStore:
    return MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536),
        settings=Settings(embedding_dim=1536),
    )


def _calendar_event(
    *,
    tenant_id: UUID,
    rep_id: UUID,
    title: str = "Discovery Call",
    status: CalendarEventStatus = CalendarEventStatus.CONFIRMED,
    start_in_minutes: int = 60,
    rep_email: str = "rep@alex.example",
    attendees: list[CalendarAttendee] | None = None,
    calendar_event_id: str | None = None,
) -> CalendarEvent:
    start = datetime.now(timezone.utc) + timedelta(minutes=start_in_minutes)
    end = start + timedelta(minutes=30)
    return CalendarEvent(
        provider=CalendarProvider.GOOGLE,
        calendar_event_id=calendar_event_id or f"evt-{uuid4()}",
        tenant_id=tenant_id,
        rep_id=rep_id,
        rep_email=rep_email,
        title=title,
        start_at=start,
        end_at=end,
        status=status,
        organizer_email=rep_email,
        attendees=attendees
        or [
            CalendarAttendee(email=rep_email, name="Rep", is_organizer=True, response_status="accepted"),
            CalendarAttendee(email="buyer@acme.example", name="Alice Buyer", response_status="accepted"),
        ],
    )


async def _seed_crm_cache(
    *,
    memory_store: MemoryStore,
    tenant_id: UUID,
    contact_email: str = "buyer@acme.example",
    account_external_id: str = "acct-acme",
    account_domain: str = "acme.example",
) -> None:
    """Write the two CRM memory rows the classifier joins against."""
    import json

    contact_record = CRMRecord(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.CONTACT,
        external_id="contact-1",
        name="Alice Buyer",
        email=contact_email,
        account_external_id=account_external_id,
    )
    account_record = CRMRecord(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.ACCOUNT,
        external_id=account_external_id,
        name="Acme Inc",
        domain=account_domain,
    )
    await memory_store.write_with_status(
        tenant_id=tenant_id,
        write=MemoryWrite(
            tier=MemoryTier.ORG,
            kind="crm.contact",
            content=json.dumps(contact_record.model_dump(mode="json"), default=str),
            attributes={
                "crm_platform": "hubspot",
                "crm_kind": "contact",
                "crm_external_id": "contact-1",
                "account_external_id": account_external_id,
            },
        ),
        index_embeddings=False,
    )
    await memory_store.write_with_status(
        tenant_id=tenant_id,
        write=MemoryWrite(
            tier=MemoryTier.ORG,
            kind="crm.account",
            content=json.dumps(account_record.model_dump(mode="json"), default=str),
            attributes={
                "crm_platform": "hubspot",
                "crm_kind": "account",
                "crm_external_id": account_external_id,
            },
        ),
        index_embeddings=False,
    )


@pytest.mark.asyncio
async def test_classify_emits_detected_for_external_meeting(tenant: UUID, rep: UUID):
    store = _store()
    await _seed_crm_cache(memory_store=store, tenant_id=tenant)
    bus = EventBus()
    seen: list[MeetingDetected] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_DETECTED, listener)
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    result = await classifier.classify(_calendar_event(tenant_id=tenant, rep_id=rep))

    assert result is not None
    assert result.is_external is True
    assert seen and seen[0].calendar_event_id == result.calendar_event_id
    # CRM resolution found the account from the contact join.
    assert result.account_external_id == "acct-acme"
    # External attendee profile carries the CRM linkage.
    external = next(p for p in result.attendee_profiles if p.email == "buyer@acme.example")
    assert external.is_external is True
    assert external.crm_contact_external_id == "contact-1"
    assert external.crm_account_external_id == "acct-acme"


@pytest.mark.asyncio
async def test_classify_skips_internal_only_meeting(tenant: UUID, rep: UUID):
    """Both attendees on the rep's domain → not external → no event."""
    store = _store()
    bus = EventBus()
    seen: list[MeetingDetected] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_DETECTED, listener)
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    event = _calendar_event(
        tenant_id=tenant,
        rep_id=rep,
        rep_email="rep@alex.example",
        attendees=[
            CalendarAttendee(email="rep@alex.example", name="Rep", is_organizer=True),
            CalendarAttendee(email="manager@alex.example", name="Manager"),
        ],
    )
    result = await classifier.classify(event)
    assert result is None
    assert seen == []


@pytest.mark.asyncio
async def test_classify_emits_detected_with_null_opportunity_when_no_match(
    tenant: UUID, rep: UUID
):
    """Per the blueprint: no CRM match → still emit, with null linkage."""
    store = _store()
    # Note: no CRM cache seeded.
    bus = EventBus()
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    result = await classifier.classify(_calendar_event(tenant_id=tenant, rep_id=rep))
    assert result is not None
    assert result.is_external is True
    assert result.opportunity_external_id is None
    assert result.account_external_id is None
    # The external attendee is still in the profile list, just unresolved.
    external = next(p for p in result.attendee_profiles if p.email == "buyer@acme.example")
    assert external.is_external is True
    assert external.crm_account_external_id is None


@pytest.mark.asyncio
async def test_trigger_at_is_max_of_start_minus_30_or_now(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    fixed_now = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)
    classifier = MeetingClassifier(
        memory_store=store,
        emitter=MeetingEventEmitter(bus),
        now=lambda: fixed_now,
    )

    # Case A: meeting starts in 60 minutes → trigger at start - 30.
    event_a = _calendar_event(tenant_id=tenant, rep_id=rep, start_in_minutes=60)
    # Override start_at to be deterministic relative to fixed_now.
    event_a = event_a.model_copy(
        update={
            "start_at": fixed_now + timedelta(minutes=60),
            "end_at": fixed_now + timedelta(minutes=90),
        }
    )
    result_a = await classifier.classify(event_a)
    assert result_a is not None
    assert result_a.trigger_at == fixed_now + timedelta(minutes=30)

    # Case B: meeting starts in 10 minutes → trigger at now (immediate).
    event_b = _calendar_event(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-immediate",
    ).model_copy(
        update={
            "start_at": fixed_now + timedelta(minutes=10),
            "end_at": fixed_now + timedelta(minutes=40),
        }
    )
    result_b = await classifier.classify(event_b)
    assert result_b is not None
    assert result_b.trigger_at == fixed_now


@pytest.mark.asyncio
async def test_cancellation_after_detected_emits_cancelled(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    seen: list[MeetingCancelled] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_CANCELLED, listener)
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    event = _calendar_event(
        tenant_id=tenant, rep_id=rep, calendar_event_id="evt-to-cancel"
    )
    detected = await classifier.classify(event)
    assert detected is not None

    cancelled_event = event.model_copy(update={"status": CalendarEventStatus.CANCELLED})
    result = await classifier.classify(cancelled_event)
    assert result is None
    assert seen and seen[0].calendar_event_id == "evt-to-cancel"


@pytest.mark.asyncio
async def test_cancellation_without_prior_detection_is_noop(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_CANCELLED, listener)
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    cancelled_event = _calendar_event(
        tenant_id=tenant,
        rep_id=rep,
        status=CalendarEventStatus.CANCELLED,
        calendar_event_id="evt-never-seen",
    )
    result = await classifier.classify(cancelled_event)
    assert result is None
    assert seen == []


@pytest.mark.asyncio
async def test_double_cancellation_does_not_double_emit(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    seen: list = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe(TOPIC_MEETING_CANCELLED, listener)
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    event = _calendar_event(
        tenant_id=tenant, rep_id=rep, calendar_event_id="evt-dup-cancel"
    )
    await classifier.classify(event)
    cancelled = event.model_copy(update={"status": CalendarEventStatus.CANCELLED})
    await classifier.classify(cancelled)
    await classifier.classify(cancelled)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_classify_persists_calendar_event_and_state_rows(tenant: UUID, rep: UUID):
    store = _store()
    bus = EventBus()
    classifier = MeetingClassifier(memory_store=store, emitter=MeetingEventEmitter(bus))

    event = _calendar_event(
        tenant_id=tenant, rep_id=rep, calendar_event_id="evt-persist"
    )
    await classifier.classify(event)

    event_rows = await store.list_recent(
        tenant_id=tenant,
        tier=MemoryTier.ORG,
        owner_id=None,
        kinds_filter=["calendar.event"],
        limit=10,
    )
    state_rows = await store.list_recent(
        tenant_id=tenant,
        tier=MemoryTier.ORG,
        owner_id=None,
        kinds_filter=["calendar.event_state"],
        limit=10,
    )
    assert any(r.attributes.get("calendar_event_id") == "evt-persist" for r in event_rows)
    detected_states = [
        r for r in state_rows
        if r.attributes.get("calendar_event_id") == "evt-persist"
        and r.attributes.get("lifecycle_state") == CalendarLifecycleState.DETECTED.value
    ]
    assert len(detected_states) == 1
