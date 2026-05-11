from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Provide deterministic defaults so the FastAPI app boots without prod env."""
    os.environ.setdefault("SLACK_SIGNING_SECRET", "")
    os.environ.setdefault("SLACK_BOT_TOKEN", "")
    os.environ.setdefault("ALEX_WEBHOOK_SECRET", "")


class FakeSlackClient:
    """Minimal AsyncWebClient stand-in for /deliver tests."""

    def __init__(self) -> None:
        self.posted_messages: list[dict[str, Any]] = []
        self.opened_users: list[str] = []
        self.dm_channel_id = "D-fake"

    async def conversations_open(self, *, users: str) -> dict[str, Any]:
        self.opened_users.append(users)
        return {"channel": {"id": self.dm_channel_id}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted_messages.append(kwargs)
        return {"ok": True, "ts": "1716123456.000100"}


@pytest_asyncio.fixture
async def app() -> AsyncIterator[Any]:
    """Build the FastAPI app with the Bolt internals stubbed for tests."""
    from alex_slack_bot.main import create_app

    fastapi_app = create_app()
    fake = FakeSlackClient()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        # Bolt's `.client` property is read-only; the private attr is what
        # the property returns, so we swap that for the fake.
        fastapi_app.state.bolt_app._async_client = fake  # type: ignore[attr-defined]
        fastapi_app.state.fake_slack_client = fake
        yield fastapi_app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
