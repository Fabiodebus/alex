"""Integration tests for the Phase 4 proactive composers.

We exercise each composer with a controllable AgentBackend (so JSON
output is deterministic) and the StubMessagingDeliveryClient so the
rep-facing cards land somewhere we can assert on.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from alex_agent_runtime.config import Settings
from alex_agent_runtime.schemas import (
    AgentResponse,
    AttendeeProfile,
    CRMNoteReview,
    CRMPlatform,
    CRMRecord,
    CRMRecordKind,
    CalendarAttendee,
    CalendarEvent,
    CalendarEventStatus,
    CalendarProvider,
    DeliveryChannel,
    EmailSendRequest,
    EmailSendResult,
    FollowUpDraft,
    MeetingBrief,
    MeetingCompleted,
    MeetingDetected,
    MemoryTier,
    MemoryWrite,
    TaskApproved,
    TranscriptResult,
    VoiceLanguage,
)
from alex_agent_runtime.services.approval_gate import ApprovalGate
from alex_agent_runtime.services.approval_handler import TOPIC_APPROVAL_APPROVED
from alex_agent_runtime.services.approved_action_dispatcher import (
    ApprovedActionDispatcher,
    attach_dispatcher,
)
from alex_agent_runtime.services.crm_fetch_client import StubCRMFetchClient
from alex_agent_runtime.services.crm_note_composer import CRMNoteComposer
from alex_agent_runtime.services.crm_reader import CRMReader
from alex_agent_runtime.services.crm_validator import CRMValidator
from alex_agent_runtime.services.crm_write_client import StubCRMWriteClient
from alex_agent_runtime.services.crm_writer import CRMWriter
from alex_agent_runtime.services.delivery_preferences import DeliveryPreferenceRepo
from alex_agent_runtime.services.delivery_tracker import DeliveryTracker
from alex_agent_runtime.services.email_send_client import StubEmailSendClient
from alex_agent_runtime.services.embedding_client import StubEmbeddingClient
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.services.follow_up_draft_composer import FollowUpDraftComposer
from alex_agent_runtime.services.meeting_brief_composer import MeetingBriefComposer
from alex_agent_runtime.services.memory_store import MemoryStore
from alex_agent_runtime.services.messaging_delivery_client import (
    StubMessagingDeliveryClient,
)
from alex_agent_runtime.services.output_router import OutputRouter
from alex_agent_runtime.services.tenant_flags import (
    FLAG_MEDDIC_ENABLED,
    TenantFlagRepo,
)
from alex_agent_runtime.services.transcript_fetcher import StubTranscriptFetcher
from alex_agent_runtime.services.voice_applicator import VoiceApplicator
from alex_agent_runtime.services.voice_profile_store import VoiceProfileStore
from alex_agent_runtime.tenant_context import tenant_scope


class _ScriptedBackend:
    """AgentBackend that returns whatever JSON string we hand it."""

    name = "scripted"

    def __init__(self, response_text: str = "{}") -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.response_text = response_text

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        max_turns: int = 1,
    ) -> AgentResponse:
        self.calls.append((prompt, system_prompt))
        return AgentResponse(text=self.response_text, backend=self.name)


def _build_world() -> dict[str, Any]:
    settings = Settings(embedding_dim=1536)
    memory_store = MemoryStore(
        embedding_client=StubEmbeddingClient(dim=1536), settings=settings
    )
    delivery_client = StubMessagingDeliveryClient()
    tracker = DeliveryTracker(escalation_seconds=1800)
    router = OutputRouter(
        delivery_client=delivery_client,
        preferences=DeliveryPreferenceRepo(),
        tracker=tracker,
    )
    return {
        "settings": settings,
        "memory_store": memory_store,
        "delivery_client": delivery_client,
        "router": router,
    }


def _delivered_titles(client: StubMessagingDeliveryClient) -> list[str]:
    return [attempt.title for _channel, attempt in client.calls]


# ---------------------------------------------------------------------------
# MeetingBriefComposer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_brief_composer_writes_card_and_idempotency_marker(
    tenant: UUID, rep: UUID
):
    world = _build_world()
    backend = _ScriptedBackend(json.dumps({
        "account_context": "Acme Inc — DACH mid-market manufacturer.",
        "attendee_profiles": [{"email": "buyer@acme.example", "role": "Director", "context": "Met 2x"}],
        "last_touch_summary": "Demo last Tuesday.",
        "open_commitments": ["Send security questionnaire by Friday"],
        "talking_points": ["Confirm budget owner", "Review SLA", "Time-to-value"],
        "recommended_cta": "Book the technical deep-dive.",
        "meddic_gaps": [],
    }))
    composer = MeetingBriefComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        crm_reader=CRMReader(
            memory_store=world["memory_store"], fetch_client=StubCRMFetchClient()
        ),
        output_router=world["router"],
        tenant_flags=TenantFlagRepo(),
        settings=world["settings"],
    )

    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    detected = MeetingDetected(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-brief-1",
        provider=CalendarProvider.GOOGLE,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        trigger_at=datetime.now(timezone.utc),
        title="Discovery — Acme",
        is_external=True,
        attendee_profiles=[
            AttendeeProfile(email="rep@alex.example", is_external=False),
            AttendeeProfile(
                email="buyer@acme.example",
                is_external=True,
                crm_contact_external_id="contact-1",
                crm_account_external_id="acct-1",
            ),
        ],
        account_external_id="acct-1",
    )

    brief = await composer.compose(detected)
    assert isinstance(brief, MeetingBrief)
    assert brief.recommended_cta == "Book the technical deep-dive."
    assert "Confirm budget owner" in brief.talking_points
    titles = _delivered_titles(world["delivery_client"])
    assert any("Meeting prep" in t for t in titles)

    # Idempotency: re-compose drops the second call.
    again = await composer.compose(detected)
    assert again is None


@pytest.mark.asyncio
async def test_brief_flags_unknown_attendees(tenant: UUID, rep: UUID):
    world = _build_world()
    backend = _ScriptedBackend(json.dumps({}))  # empty JSON: forces fallbacks
    composer = MeetingBriefComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        crm_reader=CRMReader(
            memory_store=world["memory_store"], fetch_client=StubCRMFetchClient()
        ),
        output_router=world["router"],
        tenant_flags=TenantFlagRepo(),
        settings=world["settings"],
    )

    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    detected = MeetingDetected(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-unknown",
        provider=CalendarProvider.GOOGLE,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        trigger_at=datetime.now(timezone.utc),
        title="Intro chat",
        is_external=True,
        attendee_profiles=[
            AttendeeProfile(email="rep@alex.example", is_external=False),
            AttendeeProfile(email="stranger@unknown.example", is_external=True),
        ],
    )
    brief = await composer.compose(detected)
    assert isinstance(brief, MeetingBrief)
    assert "stranger@unknown.example" in brief.flagged_unknown_attendees


# ---------------------------------------------------------------------------
# FollowUpDraftComposer
# ---------------------------------------------------------------------------
async def _seed_calendar_event(
    *,
    memory_store: MemoryStore,
    tenant_id: UUID,
    rep_id: UUID,
    calendar_event_id: str,
    rep_email: str = "rep@alex.example",
    attendees: list[CalendarAttendee] | None = None,
) -> None:
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    event = CalendarEvent(
        provider=CalendarProvider.GOOGLE,
        calendar_event_id=calendar_event_id,
        tenant_id=tenant_id,
        rep_id=rep_id,
        rep_email=rep_email,
        title="Discovery — Acme",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=CalendarEventStatus.CONFIRMED,
        attendees=attendees
        or [
            CalendarAttendee(email=rep_email, is_organizer=True),
            CalendarAttendee(email="buyer@acme.example"),
        ],
    )
    await memory_store.write_with_status(
        tenant_id=tenant_id,
        write=MemoryWrite(
            tier=MemoryTier.ORG,
            kind="calendar.event",
            content=json.dumps(event.model_dump(mode="json"), default=str),
            attributes={
                "calendar_event_id": calendar_event_id,
                "rep_id": str(rep_id),
                "rep_email": rep_email,
                "is_external": True,
                "start_at": event.start_at.isoformat(),
                "end_at": event.end_at.isoformat(),
            },
        ),
        index_embeddings=False,
    )


@pytest.mark.asyncio
async def test_follow_up_composer_opens_email_send_task(
    tenant: UUID, rep: UUID
):
    world = _build_world()
    await _seed_calendar_event(
        memory_store=world["memory_store"],
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-fu-1",
    )
    backend = _ScriptedBackend(json.dumps({
        "subject": "Quick follow-up — Discovery",
        "body": "Hi Sam,\n\nThanks for the time today.\n\nBest regards",
        "to": ["buyer@acme.example"],
    }))
    bus = EventBus()
    gate = ApprovalGate(event_bus=bus)
    voice_store = VoiceProfileStore(memory_store=world["memory_store"])
    composer = FollowUpDraftComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        transcript_fetcher=StubTranscriptFetcher(),
        voice_applicator=VoiceApplicator(store=voice_store),
        approval_gate=gate,
        output_router=world["router"],
    )

    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-fu-1",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Discovery — Acme",
    )

    with tenant_scope(tenant):
        draft = await composer.compose(completed)
    assert isinstance(draft, FollowUpDraft)
    assert draft.subject == "Quick follow-up — Discovery"
    assert "buyer@acme.example" in draft.to
    titles = _delivered_titles(world["delivery_client"])
    assert any("Follow-up ready" in t for t in titles)


@pytest.mark.asyncio
async def test_follow_up_composer_pauses_on_multi_company(
    tenant: UUID, rep: UUID
):
    world = _build_world()
    await _seed_calendar_event(
        memory_store=world["memory_store"],
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-fu-multi",
        attendees=[
            CalendarAttendee(email="rep@alex.example", is_organizer=True),
            CalendarAttendee(email="buyer@acme.example"),
            CalendarAttendee(email="partner@globex.example"),
        ],
    )
    backend = _ScriptedBackend("{}")
    voice_store = VoiceProfileStore(memory_store=world["memory_store"])
    composer = FollowUpDraftComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        transcript_fetcher=StubTranscriptFetcher(),
        voice_applicator=VoiceApplicator(store=voice_store),
        approval_gate=ApprovalGate(event_bus=EventBus()),
        output_router=world["router"],
    )
    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-fu-multi",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Three-party check-in",
    )
    with tenant_scope(tenant):
        draft = await composer.compose(completed)
    assert isinstance(draft, FollowUpDraft)
    assert draft.multi_company_pending is True
    assert any(
        "Follow-up paused" in t for t in _delivered_titles(world["delivery_client"])
    )
    # No model call was made for the draft body in pause mode? Actually the
    # composer DOES call the model first (we build the prompt before wrap),
    # but it discards the result. The wrap path is what handles the pause.
    # So a single call is fine; pause is reflected in the FollowUpDraft.


@pytest.mark.asyncio
async def test_email_send_dispatched_after_approval(tenant: UUID, rep: UUID):
    """End-to-end: composer opens task → approve → dispatcher sends."""
    world = _build_world()
    await _seed_calendar_event(
        memory_store=world["memory_store"],
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-end-to-end",
    )
    backend = _ScriptedBackend(json.dumps({
        "subject": "Quick follow-up",
        "body": "Hi Sam,\n\nThanks for the time.\n\nBest regards",
        "to": ["buyer@acme.example"],
    }))
    bus = EventBus()
    gate = ApprovalGate(event_bus=bus)
    voice_store = VoiceProfileStore(memory_store=world["memory_store"])
    email_client = StubEmailSendClient()
    dispatcher = ApprovedActionDispatcher(
        crm_writer=CRMWriter(write_client=StubCRMWriteClient()),
        crm_validator=CRMValidator(),
        email_send_client=email_client,
    )
    attach_dispatcher(bus=bus, dispatcher=dispatcher)
    composer = FollowUpDraftComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        transcript_fetcher=StubTranscriptFetcher(),
        voice_applicator=VoiceApplicator(store=voice_store),
        approval_gate=gate,
        output_router=world["router"],
    )

    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-end-to-end",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Discovery",
    )
    with tenant_scope(tenant):
        await composer.compose(completed)

    # Pull the task_id out of the most recent task_state row for this rep
    # and publish an approval.approved.
    from sqlalchemy import text

    from alex_agent_runtime.db import admin_session

    async with admin_session() as session:
        row = await session.execute(
            text(
                "SELECT id, payload FROM task_state "
                "WHERE tenant_id = :t AND assignee_rep_id = :r "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": str(tenant), "r": str(rep)},
        )
        record = row.mappings().one()
    payload = record["payload"]
    await bus.publish(
        TOPIC_APPROVAL_APPROVED,
        TaskApproved(
            tenant_id=tenant,
            rep_id=rep,
            task_id=record["id"],
            task_type="email.send",
            payload=payload,
        ),
    )

    assert len(email_client.calls) == 1
    sent = email_client.calls[0]
    assert sent.to == ["buyer@acme.example"]
    assert sent.subject == "Quick follow-up"


# ---------------------------------------------------------------------------
# CRMNoteComposer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crm_note_composer_packages_note_and_field_updates(
    tenant: UUID, rep: UUID
):
    world = _build_world()
    backend = _ScriptedBackend(json.dumps({
        "summary": "Buyer wants EU residency and clean Teams integration.",
        "key_points": ["EU data residency", "Teams integration", "Clear pricing"],
        "decisions": ["Loop in security lead next call"],
        "next_steps": ["Send one-pager + reference customer"],
        "new_contacts": [
            {"name": "Sec Lead", "email": "sec@acme.example", "role": "Security"}
        ],
        "stage_change_proposal": {
            "from": "qualification",
            "to": "Presentation",
            "evidence": "Buyer agreed to demo schedule.",
        },
        "field_updates": [
            {
                "field_name": "amount",
                "current_value": 0,
                "proposed_value": 75000,
                "reason": "Buyer mentioned 250 seats * 300 EUR.",
            }
        ],
        "meddic_mappings": [],
        "meddic_gaps": [],
        "note_body": "Recap: EU residency and Teams integration are the main concerns.",
    }))
    bus = EventBus()
    gate = ApprovalGate(event_bus=bus)
    flags = TenantFlagRepo()
    composer = CRMNoteComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        crm_reader=CRMReader(
            memory_store=world["memory_store"], fetch_client=StubCRMFetchClient()
        ),
        crm_validator=CRMValidator(),
        approval_gate=gate,
        output_router=world["router"],
        transcript_fetcher=StubTranscriptFetcher(),
        tenant_flags=flags,
    )
    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-crm-1",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Discovery — Acme",
        opportunity_external_id="deal-1",
        crm_platform=CRMPlatform.HUBSPOT,
    )
    with tenant_scope(tenant):
        review = await composer.compose(completed)
    assert isinstance(review, CRMNoteReview)
    # Validator accepts the dealstage transition (Presentation) + amount.
    field_names = {u["field_name"] for u in review.field_updates}
    assert "dealstage" in field_names
    assert "amount" in field_names
    assert review.note_body.startswith("Recap")
    assert any(
        "CRM review ready" in t for t in _delivered_titles(world["delivery_client"])
    )


@pytest.mark.asyncio
async def test_crm_note_composer_skips_meddic_when_flag_off(
    tenant: UUID, rep: UUID
):
    """meddic_enabled default is False; mappings/gaps are dropped."""
    world = _build_world()
    backend = _ScriptedBackend(json.dumps({
        "summary": "Recap",
        "meddic_mappings": [{"letter": "E", "value": "CFO confirmed"}],
        "meddic_gaps": ["No metrics yet"],
        "note_body": "Body",
    }))
    composer = CRMNoteComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        crm_reader=CRMReader(
            memory_store=world["memory_store"], fetch_client=StubCRMFetchClient()
        ),
        crm_validator=CRMValidator(),
        approval_gate=ApprovalGate(event_bus=EventBus()),
        output_router=world["router"],
        transcript_fetcher=StubTranscriptFetcher(),
        tenant_flags=TenantFlagRepo(),  # flag off
    )
    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-crm-no-meddic",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Discovery",
        opportunity_external_id="deal-X",
        crm_platform=CRMPlatform.HUBSPOT,
    )
    with tenant_scope(tenant):
        review = await composer.compose(completed)
    assert review.meddic_mappings == []
    assert review.meddic_gaps == []


@pytest.mark.asyncio
async def test_crm_note_composer_includes_meddic_when_flag_on(
    tenant: UUID, rep: UUID
):
    world = _build_world()
    backend = _ScriptedBackend(json.dumps({
        "summary": "Recap",
        "meddic_mappings": [{"letter": "E", "value": "CFO confirmed"}],
        "meddic_gaps": ["No metrics yet"],
        "note_body": "Body",
    }))
    flags = TenantFlagRepo()
    await flags.set_bool(tenant_id=tenant, flag=FLAG_MEDDIC_ENABLED, enabled=True)

    composer = CRMNoteComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        crm_reader=CRMReader(
            memory_store=world["memory_store"], fetch_client=StubCRMFetchClient()
        ),
        crm_validator=CRMValidator(),
        approval_gate=ApprovalGate(event_bus=EventBus()),
        output_router=world["router"],
        transcript_fetcher=StubTranscriptFetcher(),
        tenant_flags=flags,
    )
    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-crm-with-meddic",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Qualification",
        opportunity_external_id="deal-Y",
        crm_platform=CRMPlatform.HUBSPOT,
    )
    with tenant_scope(tenant):
        review = await composer.compose(completed)
    assert review.meddic_mappings and review.meddic_mappings[0]["letter"] == "E"
    assert "No metrics yet" in review.meddic_gaps


@pytest.mark.asyncio
async def test_crm_note_composer_skips_when_no_opportunity(
    tenant: UUID, rep: UUID
):
    world = _build_world()
    backend = _ScriptedBackend("{}")
    composer = CRMNoteComposer(
        agent_backend=backend,
        memory_store=world["memory_store"],
        crm_reader=CRMReader(
            memory_store=world["memory_store"], fetch_client=StubCRMFetchClient()
        ),
        crm_validator=CRMValidator(),
        approval_gate=ApprovalGate(event_bus=EventBus()),
        output_router=world["router"],
        transcript_fetcher=StubTranscriptFetcher(),
        tenant_flags=TenantFlagRepo(),
    )
    completed = MeetingCompleted(
        tenant_id=tenant,
        rep_id=rep,
        calendar_event_id="evt-crm-no-opp",
        provider=CalendarProvider.GOOGLE,
        start_at=datetime.now(timezone.utc) - timedelta(hours=1),
        end_at=datetime.now(timezone.utc) - timedelta(minutes=30),
        title="Discovery",
        # opportunity_external_id and crm_platform intentionally None
    )
    await composer.handle_meeting_completed(completed)
    # No card delivered, no model call needed.
    assert backend.calls == []
