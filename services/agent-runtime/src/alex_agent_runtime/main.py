"""FastAPI application entry point.

Wires together the DB engine, AgentBackend, FeatureRouter, EventProcessor,
ApprovalHandler, and SchedulerService inside a single lifespan so all
components share a deterministic startup/shutdown order.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI

from .config import get_settings
from .db import dispose_engine, init_engine
from .jobs.heartbeat import heartbeat
from .middleware import TenantHeaderMiddleware, WebhookSignatureMiddleware
from .routes.callbacks import router as callbacks_router
from .routes.connections import router as connections_router
from .routes.events import router as events_router
from .routes.health import router as health_router
from .routes.ingestion import router as ingestion_router
from .routes.onboarding import router as onboarding_router
from .services.agent_backend import build_default_backend
from .services.approval_expiry_scan import (
    APPROVAL_EXPIRY_SCAN_INTERVAL_SECONDS,
    ApprovalExpiryScan,
    build_expiry_scan_job,
)
from .services.approval_gate import ApprovalGate
from .services.approval_handler import ApprovalHandler
from .services.approved_action_dispatcher import (
    ApprovedActionDispatcher,
    attach_dispatcher,
)
from .services.crm_fetch_client import build_default_crm_fetch_client
from .services.crm_reader import CRMReader
from .services.crm_validator import CRMValidator
from .services.crm_write_client import build_default_crm_write_client
from .services.crm_writer import CRMWriter
from .services.delivery_escalation_scan import (
    DELIVERY_ESCALATION_SCAN_INTERVAL_SECONDS,
    DeliveryEscalationScan,
    build_escalation_scan_job,
)
from .services.delivery_preferences import DeliveryPreferenceRepo
from .services.delivery_tracker import DeliveryTracker
from .services.messaging_delivery_client import (
    build_default_messaging_delivery_client,
)
from .services.output_router import OutputRouter, attach_router
from .services.activation_tracker import (
    ActivationTracker,
    attach_activation_tracker,
    build_activation_scan_job,
)
from .services.oauth_orchestrator import OAuthOrchestrator
from .services.oauth_provider import build_default_oauth_provider
from .services.onboarding_conversation import OnboardingConversationFlow
from .services.onboarding_state_repo import OnboardingStateRepo
from .services.crm_note_composer import CRMNoteComposer
from .services.email_send_client import build_default_email_send_client
from .services.follow_up_draft_composer import FollowUpDraftComposer
from .services.meeting_brief_composer import MeetingBriefComposer
from .services.meeting_brief_scan import (
    MeetingBriefScan,
    build_meeting_brief_scan_job,
)
from .services.meeting_events import (
    TOPIC_MEETING_COMPLETED,
    TOPIC_MEETING_DETECTED,
)
from .services.tenant_flags import TenantFlagRepo
from .services.transcript_fetcher import build_default_transcript_fetcher
from .services.voice_applicator import VoiceApplicator
from .services.voice_profile_store import VoiceProfileStore
from .services.voice_signal_extractor import VoiceSignalExtractor
from .services.voice_updater import VoiceUpdater, attach_updater
from .services.meeting_classifier import MeetingClassifier
from .services.meeting_completion_scan import (
    MEETING_COMPLETION_SCAN_INTERVAL_SECONDS,
    MeetingCompletionScan,
    build_completion_scan_job,
)
from .services.meeting_events import MeetingEventEmitter
from .services.embedding_client import build_default_embedding_client
from .services.event_bus import EventBus
from .services.event_processor import EventProcessor
from .services.feature_router import FeatureRouter
from .services.ingestion_pipeline import IngestionPipeline
from .services.ingestion_provider import build_default_ingestion_provider
from .services.memory_store import MemoryStore
from .services.memory_summarizer import MemorySummarizer
from .services.scheduler import SchedulerService


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    settings = get_settings()
    log = structlog.get_logger(__name__)
    log.info("runtime.starting", host=settings.agent_runtime_host, port=settings.agent_runtime_port)

    init_engine(settings)

    feature_router = FeatureRouter()
    agent_backend = build_default_backend(settings)
    event_processor = EventProcessor(feature_router)
    scheduler = SchedulerService()
    event_bus = EventBus()
    approval_gate = ApprovalGate(event_bus=event_bus)
    approval_handler = ApprovalHandler(event_bus=event_bus)
    approval_expiry_scan = ApprovalExpiryScan(event_bus=event_bus)
    embedding_client = build_default_embedding_client(settings)
    memory_store = MemoryStore(embedding_client=embedding_client, settings=settings)
    memory_summarizer = MemorySummarizer(
        memory_store=memory_store,
        agent_backend=agent_backend,
        event_bus=event_bus,
        settings=settings,
    )
    ingestion_provider = build_default_ingestion_provider(settings)
    ingestion_pipeline = IngestionPipeline(
        provider=ingestion_provider,
        memory_store=memory_store,
        event_bus=event_bus,
        settings=settings,
    )
    crm_fetch_client = build_default_crm_fetch_client(settings)
    crm_reader = CRMReader(memory_store=memory_store, fetch_client=crm_fetch_client)
    feature_router.register("crm.activity_logged", crm_reader.handle_data_sync)
    crm_validator = CRMValidator()
    crm_write_client = build_default_crm_write_client(settings)
    crm_writer = CRMWriter(write_client=crm_write_client, event_bus=event_bus)
    email_send_client = build_default_email_send_client(settings)
    approved_action_dispatcher = ApprovedActionDispatcher(
        crm_writer=crm_writer,
        crm_validator=crm_validator,
        email_send_client=email_send_client,
    )
    attach_dispatcher(bus=event_bus, dispatcher=approved_action_dispatcher)
    delivery_preferences = DeliveryPreferenceRepo()
    delivery_tracker = DeliveryTracker(
        escalation_seconds=settings.delivery_escalation_seconds,
    )
    messaging_delivery_client = build_default_messaging_delivery_client(settings)
    output_router = OutputRouter(
        delivery_client=messaging_delivery_client,
        preferences=delivery_preferences,
        tracker=delivery_tracker,
    )
    attach_router(bus=event_bus, router=output_router)
    delivery_escalation_scan = DeliveryEscalationScan(
        tracker=delivery_tracker,
        event_bus=event_bus,
    )
    voice_profile_store = VoiceProfileStore(memory_store=memory_store)
    voice_updater = VoiceUpdater(
        store=voice_profile_store,
        extractor=VoiceSignalExtractor(),
        settings=settings,
    )
    attach_updater(bus=event_bus, updater=voice_updater)
    voice_applicator = VoiceApplicator(store=voice_profile_store)
    onboarding_state_repo = OnboardingStateRepo()
    oauth_provider = build_default_oauth_provider(settings)
    oauth_orchestrator = OAuthOrchestrator(
        provider=oauth_provider,
        state_repo=onboarding_state_repo,
    )
    onboarding_flow = OnboardingConversationFlow(
        state_repo=onboarding_state_repo,
        output_router=output_router,
    )
    activation_tracker = ActivationTracker(
        memory_store=memory_store,
        state_repo=onboarding_state_repo,
        output_router=output_router,
        event_bus=event_bus,
        settings=settings,
    )
    attach_activation_tracker(bus=event_bus, tracker=activation_tracker)
    tenant_flags = TenantFlagRepo()
    transcript_fetcher = build_default_transcript_fetcher(settings)
    meeting_brief_composer = MeetingBriefComposer(
        agent_backend=agent_backend,
        memory_store=memory_store,
        crm_reader=crm_reader,
        output_router=output_router,
        tenant_flags=tenant_flags,
        settings=settings,
    )
    event_bus.subscribe(
        TOPIC_MEETING_DETECTED, meeting_brief_composer.handle_meeting_detected
    )
    meeting_brief_scan = MeetingBriefScan(
        memory_store=memory_store,
        composer=meeting_brief_composer,
        settings=settings,
    )
    follow_up_draft_composer = FollowUpDraftComposer(
        agent_backend=agent_backend,
        memory_store=memory_store,
        transcript_fetcher=transcript_fetcher,
        voice_applicator=voice_applicator,
        approval_gate=approval_gate,
        output_router=output_router,
    )
    crm_note_composer = CRMNoteComposer(
        agent_backend=agent_backend,
        memory_store=memory_store,
        crm_reader=crm_reader,
        crm_validator=crm_validator,
        approval_gate=approval_gate,
        output_router=output_router,
        transcript_fetcher=transcript_fetcher,
        tenant_flags=tenant_flags,
    )
    event_bus.subscribe(
        TOPIC_MEETING_COMPLETED, follow_up_draft_composer.handle_meeting_completed
    )
    event_bus.subscribe(
        TOPIC_MEETING_COMPLETED, crm_note_composer.handle_meeting_completed
    )
    meeting_emitter = MeetingEventEmitter(event_bus)
    meeting_classifier = MeetingClassifier(
        memory_store=memory_store,
        emitter=meeting_emitter,
    )
    feature_router.register("calendar.update", meeting_classifier.handle_calendar_update)
    meeting_completion_scan = MeetingCompletionScan(
        memory_store=memory_store,
        emitter=meeting_emitter,
    )

    app.state.feature_router = feature_router
    app.state.agent_backend = agent_backend
    app.state.event_processor = event_processor
    app.state.approval_handler = approval_handler
    app.state.scheduler = scheduler
    app.state.event_bus = event_bus
    app.state.embedding_client = embedding_client
    app.state.memory_store = memory_store
    app.state.memory_summarizer = memory_summarizer
    app.state.ingestion_provider = ingestion_provider
    app.state.ingestion_pipeline = ingestion_pipeline
    app.state.crm_fetch_client = crm_fetch_client
    app.state.crm_reader = crm_reader
    app.state.crm_validator = crm_validator
    app.state.crm_write_client = crm_write_client
    app.state.crm_writer = crm_writer
    app.state.approval_gate = approval_gate
    app.state.approval_expiry_scan = approval_expiry_scan
    app.state.approved_action_dispatcher = approved_action_dispatcher
    app.state.delivery_preferences = delivery_preferences
    app.state.delivery_tracker = delivery_tracker
    app.state.messaging_delivery_client = messaging_delivery_client
    app.state.output_router = output_router
    app.state.delivery_escalation_scan = delivery_escalation_scan
    app.state.voice_profile_store = voice_profile_store
    app.state.voice_updater = voice_updater
    app.state.voice_applicator = voice_applicator
    app.state.onboarding_state_repo = onboarding_state_repo
    app.state.oauth_provider = oauth_provider
    app.state.oauth_orchestrator = oauth_orchestrator
    app.state.onboarding_flow = onboarding_flow
    app.state.activation_tracker = activation_tracker
    app.state.tenant_flags = tenant_flags
    app.state.transcript_fetcher = transcript_fetcher
    app.state.email_send_client = email_send_client
    app.state.meeting_brief_composer = meeting_brief_composer
    app.state.meeting_brief_scan = meeting_brief_scan
    app.state.follow_up_draft_composer = follow_up_draft_composer
    app.state.crm_note_composer = crm_note_composer
    app.state.meeting_emitter = meeting_emitter
    app.state.meeting_classifier = meeting_classifier
    app.state.meeting_completion_scan = meeting_completion_scan

    scheduler.add_interval_job(
        heartbeat,
        seconds=settings.scheduler_heartbeat_seconds,
        job_id="heartbeat",
    )
    scheduler.add_interval_job(
        build_completion_scan_job(meeting_completion_scan),
        seconds=MEETING_COMPLETION_SCAN_INTERVAL_SECONDS,
        job_id="meeting_completion_scan",
    )
    scheduler.add_interval_job(
        build_expiry_scan_job(approval_expiry_scan),
        seconds=APPROVAL_EXPIRY_SCAN_INTERVAL_SECONDS,
        job_id="approval_expiry_scan",
    )
    scheduler.add_interval_job(
        build_escalation_scan_job(delivery_escalation_scan),
        seconds=DELIVERY_ESCALATION_SCAN_INTERVAL_SECONDS,
        job_id="delivery_escalation_scan",
    )
    scheduler.add_interval_job(
        build_activation_scan_job(activation_tracker),
        seconds=settings.activation_scan_interval_seconds,
        job_id="activation_fallback_scan",
    )
    scheduler.add_interval_job(
        build_meeting_brief_scan_job(meeting_brief_scan),
        seconds=settings.meeting_brief_scan_interval_seconds,
        job_id="meeting_brief_scan",
    )
    scheduler.start()

    try:
        yield
    finally:
        log.info("runtime.stopping")
        await scheduler.shutdown()
        # Close any providers / clients that own network resources.
        for closer in (
            getattr(ingestion_provider, "close", None),
            getattr(crm_fetch_client, "close", None),
            getattr(crm_write_client, "close", None),
            getattr(messaging_delivery_client, "close", None),
            getattr(oauth_provider, "close", None),
            getattr(transcript_fetcher, "close", None),
            getattr(email_send_client, "close", None),
        ):
            if closer is not None:
                await closer()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Alex Agent Runtime", version="0.1.0", lifespan=lifespan)
    # WebhookSignatureMiddleware must run BEFORE TenantHeaderMiddleware so
    # an unsigned request is rejected without doing tenant lookup work.
    # FastAPI/Starlette runs middleware in LIFO order (last added = outermost),
    # so the TenantHeaderMiddleware is added last to appear innermost.
    app.add_middleware(TenantHeaderMiddleware)
    app.add_middleware(WebhookSignatureMiddleware)
    app.include_router(health_router)
    app.include_router(events_router)
    app.include_router(callbacks_router)
    app.include_router(connections_router)
    app.include_router(ingestion_router)
    app.include_router(onboarding_router)
    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "alex_agent_runtime.main:app",
        host=settings.agent_runtime_host,
        port=settings.agent_runtime_port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
