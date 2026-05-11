"""FastAPI entry point. Wires the Bot Framework adapter + activity handler."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI

from .activity_handler import AlexActivityHandler
from .adapter import build_adapter
from .config import get_settings
from .middleware import AlexSignatureMiddleware
from .routes.deliver import router as deliver_router
from .routes.health import router as health_router
from .routes.messages import router as messages_router
from .routes.oauth import router as oauth_router
from .services.pipedream_client import PipedreamOAuthClient
from .services.runtime_client import RuntimeClient


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
    log.info("teams_bot.starting", host=settings.teams_bot_host, port=settings.teams_bot_port)

    runtime_client = RuntimeClient(settings)
    pipedream_oauth_client = PipedreamOAuthClient(settings)
    adapter = build_adapter(settings)
    bot = AlexActivityHandler(runtime_client)

    app.state.runtime_client = runtime_client
    app.state.pipedream_oauth_client = pipedream_oauth_client
    app.state.adapter = adapter
    app.state.bot = bot

    try:
        yield
    finally:
        log.info("teams_bot.stopping")
        await runtime_client.close()
        await pipedream_oauth_client.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Alex Teams Bot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(AlexSignatureMiddleware)
    app.include_router(health_router)
    app.include_router(messages_router)
    app.include_router(deliver_router)
    app.include_router(oauth_router)
    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "alex_teams_bot.main:app",
        host=settings.teams_bot_host,
        port=settings.teams_bot_port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
