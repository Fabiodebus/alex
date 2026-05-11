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
from .services.agent_backend import build_default_backend
from .services.approval_handler import ApprovalHandler
from .services.event_processor import EventProcessor
from .services.feature_router import FeatureRouter
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
    approval_handler = ApprovalHandler()
    scheduler = SchedulerService()

    app.state.feature_router = feature_router
    app.state.agent_backend = agent_backend
    app.state.event_processor = event_processor
    app.state.approval_handler = approval_handler
    app.state.scheduler = scheduler

    scheduler.add_interval_job(
        heartbeat,
        seconds=settings.scheduler_heartbeat_seconds,
        job_id="heartbeat",
    )
    scheduler.start()

    try:
        yield
    finally:
        log.info("runtime.stopping")
        await scheduler.shutdown()
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
