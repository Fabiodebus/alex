"""FastAPI entry point. Mounts the Slack Bolt async app on /slack/events."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from .bolt_app import create_bolt_app
from .config import get_settings
from .middleware import AlexSignatureMiddleware
from .routes.deliver import router as deliver_router
from .routes.health import router as health_router
from .routes.oauth import router as oauth_router
from .services.pipedream_client import PipedreamOAuthClient


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
    log.info("slack_bot.starting", host=settings.slack_bot_host, port=settings.slack_bot_port)

    bolt_app, runtime_client = create_bolt_app(settings=settings)
    pipedream_oauth_client = PipedreamOAuthClient(settings)

    app.state.bolt_app = bolt_app
    app.state.runtime_client = runtime_client
    app.state.pipedream_oauth_client = pipedream_oauth_client
    app.state.slack_handler = AsyncSlackRequestHandler(bolt_app)

    try:
        yield
    finally:
        log.info("slack_bot.stopping")
        await runtime_client.close()
        await pipedream_oauth_client.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Alex Slack Bot", version="0.1.0", lifespan=lifespan)
    app.add_middleware(AlexSignatureMiddleware)
    app.include_router(health_router)
    app.include_router(deliver_router)
    app.include_router(oauth_router)

    @app.post("/slack/events")
    async def slack_events_handler(request: Request):  # noqa: ANN201
        handler: AsyncSlackRequestHandler = request.app.state.slack_handler
        return await handler.handle(request)

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "alex_slack_bot.main:app",
        host=settings.slack_bot_host,
        port=settings.slack_bot_port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
