from __future__ import annotations

import json
from uuid import uuid4

from alex_slack_bot.schemas import AgentOutput
from alex_slack_bot.services.block_kit import render_agent_output


def _output(**overrides):
    base = {
        "task_id": uuid4(),
        "tenant_id": uuid4(),
        "rep_id": uuid4(),
        "slack_user_id": "U123",
        "title": "Discovery call: Acme Corp",
        "body": "*Brief*: Lead from inbound demo request.",
        "metadata": {"deal_id": "deal-7", "stage": "discovery"},
    }
    base.update(overrides)
    return AgentOutput(**base)


def test_render_includes_header_section_context_actions():
    output = _output()
    blocks = render_agent_output(output)
    types = [b["type"] for b in blocks]
    assert "header" in types
    assert "section" in types
    assert "context" in types
    assert "actions" in types


def test_render_includes_one_button_per_action():
    output = _output(actions=["approve", "discard"])
    blocks = render_agent_output(output)
    actions = next(b for b in blocks if b["type"] == "actions")
    assert [el["action_id"] for el in actions["elements"]] == ["alex.approve", "alex.discard"]


def test_button_value_encodes_task_and_rep():
    output = _output(actions=["approve"])
    blocks = render_agent_output(output)
    actions = next(b for b in blocks if b["type"] == "actions")
    raw = actions["elements"][0]["value"]
    parsed = json.loads(raw)
    assert parsed["task_id"] == str(output.task_id)
    assert parsed["rep_id"] == str(output.rep_id)
    assert parsed["action"] == "approve"


def test_render_handles_metadata_caps():
    metadata = {f"k{i}": i for i in range(20)}
    blocks = render_agent_output(_output(metadata=metadata))
    context = next(b for b in blocks if b["type"] == "context")
    # Block Kit caps context elements at 10.
    assert len(context["elements"]) == 10


def test_render_omits_actions_block_when_empty():
    output = _output(actions=[])
    blocks = render_agent_output(output)
    assert all(b["type"] != "actions" for b in blocks)


def test_header_truncated_to_block_kit_limit():
    long_title = "x" * 200
    blocks = render_agent_output(_output(title=long_title))
    header_text = blocks[0]["text"]["text"]
    assert len(header_text) == 150
