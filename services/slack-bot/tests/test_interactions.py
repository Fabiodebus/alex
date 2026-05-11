"""Tests for the Bolt action handlers — direct call rather than going
through Slack's signing machinery."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest


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


def _button_body(action_id: str, *, task_id: str, rep_id: str, tenant_id: str | None = None) -> dict:
    value: dict[str, object] = {"task_id": task_id, "rep_id": rep_id, "action": action_id.split(".")[-1]}
    if tenant_id is not None:
        value["tenant_id"] = tenant_id
    return {
        "type": "block_actions",
        "team": {"id": "T-test"},
        "user": {"id": "U-test"},
        "actions": [{"action_id": action_id, "value": json.dumps(value)}],
    }


@pytest.mark.asyncio
async def test_approve_action_forwards_to_runtime():
    from alex_slack_bot.bolt_app import _handle_action
    from alex_slack_bot.schemas import CallbackAction

    runtime = FakeRuntimeClient()
    task_id = str(uuid4())
    rep_id = str(uuid4())
    tenant_id = str(uuid4())
    body = _button_body("alex.approve", task_id=task_id, rep_id=rep_id, tenant_id=tenant_id)
    await _handle_action(body=body, action=CallbackAction.APPROVE, runtime_client=runtime)

    assert len(runtime.approvals) == 1
    forwarded = runtime.approvals[0]
    assert forwarded["tenant_id"] == tenant_id
    assert str(forwarded["callback"].task_id) == task_id
    assert forwarded["callback"].action == CallbackAction.APPROVE


@pytest.mark.asyncio
async def test_handler_ignores_payload_without_value_blob():
    from alex_slack_bot.bolt_app import _handle_action
    from alex_slack_bot.schemas import CallbackAction

    runtime = FakeRuntimeClient()
    await _handle_action(
        body={"type": "block_actions", "actions": [{"action_id": "alex.discard"}]},
        action=CallbackAction.DISCARD,
        runtime_client=runtime,
    )
    assert runtime.approvals == []


@pytest.mark.asyncio
async def test_handler_falls_back_to_team_id_when_tenant_missing():
    from alex_slack_bot.bolt_app import _handle_action
    from alex_slack_bot.schemas import CallbackAction

    runtime = FakeRuntimeClient()
    body = _button_body(
        "alex.discard",
        task_id=str(uuid4()),
        rep_id=str(uuid4()),
        tenant_id=None,
    )
    await _handle_action(body=body, action=CallbackAction.DISCARD, runtime_client=runtime)
    assert runtime.approvals[0]["tenant_id"] == "T-test"
