"""Unit tests for the in-process EventBus."""
from __future__ import annotations

import pytest

from alex_agent_runtime.services.event_bus import EventBus


@pytest.mark.asyncio
async def test_publish_invokes_subscribers_in_order():
    bus = EventBus()
    received: list[str] = []

    async def handler_a(payload):
        received.append(f"a:{payload}")

    async def handler_b(payload):
        received.append(f"b:{payload}")

    bus.subscribe("topic", handler_a)
    bus.subscribe("topic", handler_b)
    await bus.publish("topic", "hello")
    assert received == ["a:hello", "b:hello"]


@pytest.mark.asyncio
async def test_publish_without_subscribers_is_a_noop():
    bus = EventBus()
    await bus.publish("nobody.listening", {"k": 1})  # must not raise


@pytest.mark.asyncio
async def test_misbehaving_handler_does_not_stall_others():
    bus = EventBus()
    survived: list[str] = []

    async def boom(_):
        raise RuntimeError("intentional")

    async def keeps_going(payload):
        survived.append(payload)

    bus.subscribe("topic", boom)
    bus.subscribe("topic", keeps_going)
    await bus.publish("topic", "ok")
    assert survived == ["ok"]


@pytest.mark.asyncio
async def test_unsubscribe_removes_handler():
    bus = EventBus()
    seen: list[str] = []

    async def handler(p):
        seen.append(p)

    bus.subscribe("topic", handler)
    bus.unsubscribe("topic", handler)
    await bus.publish("topic", "ignored")
    assert seen == []
