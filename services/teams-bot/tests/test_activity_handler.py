"""Drive AlexActivityHandler directly with synthetic activities."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from alex_teams_bot.activity_handler import AlexActivityHandler
from alex_teams_bot.schemas import CallbackAction


class FakeRuntimeClient:
    def __init__(self) -> None:
        self.approvals: list[dict] = []
        self.feedback: list[dict] = []

    async def post_approval_callback(self, *, tenant_id, callback):
        self.approvals.append({"tenant_id": tenant_id, "callback": callback})

    async def post_feedback(self, *, tenant_id, event):
        self.feedback.append({"tenant_id": tenant_id, "event": event})

    async def close(self):
        pass


class FakeTurnContext:
    def __init__(self, activity):
        self.activity = activity
        self.sent: list[object] = []

    async def send_activity(self, message):
        self.sent.append(message)
        return SimpleNamespace(id="m-1")


def _submit_activity(*, action: str, tenant_id: str | None = None, rating: int | None = None):
    alex_payload: dict[str, object] = {
        "task_id": str(uuid4()),
        "rep_id": str(uuid4()),
        "action": action,
    }
    if tenant_id is not None:
        alex_payload["tenant_id"] = tenant_id
    if rating is not None:
        alex_payload["rating"] = rating
    return SimpleNamespace(
        type="message",
        text=None,
        value={"alex": alex_payload},
    )


@pytest.mark.asyncio
async def test_approve_submit_forwards_to_runtime():
    runtime = FakeRuntimeClient()
    handler = AlexActivityHandler(runtime)
    tenant_id = str(uuid4())
    activity = _submit_activity(action="approve", tenant_id=tenant_id)
    await handler.on_turn(FakeTurnContext(activity))
    assert len(runtime.approvals) == 1
    forwarded = runtime.approvals[0]
    assert forwarded["tenant_id"] == tenant_id
    assert forwarded["callback"].action == CallbackAction.APPROVE


@pytest.mark.asyncio
async def test_feedback_submit_forwards_feedback_event():
    runtime = FakeRuntimeClient()
    handler = AlexActivityHandler(runtime)
    tenant_id = str(uuid4())
    activity = _submit_activity(action="feedback", tenant_id=tenant_id, rating=-1)
    await handler.on_turn(FakeTurnContext(activity))
    assert len(runtime.feedback) == 1
    assert runtime.feedback[0]["event"].rating == -1


@pytest.mark.asyncio
async def test_unknown_payload_is_ignored():
    runtime = FakeRuntimeClient()
    handler = AlexActivityHandler(runtime)
    activity = SimpleNamespace(
        type="message",
        text=None,
        value={"not_alex": True},
    )
    await handler.on_turn(FakeTurnContext(activity))
    assert runtime.approvals == []
    assert runtime.feedback == []


@pytest.mark.asyncio
async def test_text_message_replies_with_help():
    runtime = FakeRuntimeClient()
    handler = AlexActivityHandler(runtime)
    activity = SimpleNamespace(type="message", text="help", value=None)
    ctx = FakeTurnContext(activity)
    await handler.on_turn(ctx)
    assert ctx.sent and "Alex" in ctx.sent[0]
