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
    approved_action_dispatcher = ApprovedActionDispatcher(
        crm_writer=crm_writer,
        crm_validator=crm_validator,
    )
    attach_dispatcher(bus=event_bus, dispatcher=approved_action_dispatcher)
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
