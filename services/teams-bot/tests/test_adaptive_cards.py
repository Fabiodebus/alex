from __future__ import annotations

from uuid import uuid4

from alex_teams_bot.schemas import AgentOutput
from alex_teams_bot.services.adaptive_cards import (
    ADAPTIVE_CARD_CONTENT_TYPE,
    render_agent_output,
)


def _output(**overrides):
    base = {
        "task_id": uuid4(),
        "tenant_id": uuid4(),
        "rep_id": uuid4(),
        "conversation_reference": {"channel_id": "msteams"},
        "title": "Brief: Acme Corp",
        "body": "Stage: Discovery",
        "metadata": {"deal_id": "d-1", "stage": "discovery"},
    }
    base.update(overrides)
    return AgentOutput(**base)


def test_attachment_has_adaptive_card_content_type():
    attachment = render_agent_output(_output())
    assert attachment["contentType"] == ADAPTIVE_CARD_CONTENT_TYPE
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.5"


def test_body_contains_title_and_body_blocks():
    attachment = render_agent_output(_output(title="Hello", body="World"))
    card = attachment["content"]
    block_types = [b["type"] for b in card["body"]]
    assert block_types[:2] == ["TextBlock", "TextBlock"]
    assert card["body"][0]["text"] == "Hello"
    assert card["body"][1]["text"] == "World"


def test_metadata_renders_as_factset():
    attachment = render_agent_output(_output())
    facts = next(b for b in attachment["content"]["body"] if b["type"] == "FactSet")
    titles = [f["title"] for f in facts["facts"]]
    assert "deal_id" in titles and "stage" in titles


def test_actions_carry_alex_payload():
    output = _output(actions=["approve", "discard"])
    card = render_agent_output(output)["content"]
    actions = card["actions"]
    assert [a["id"] for a in actions] == ["alex.approve", "alex.discard"]
    payload = actions[0]["data"]["alex"]
    assert payload["task_id"] == str(output.task_id)
    assert payload["rep_id"] == str(output.rep_id)
    assert payload["tenant_id"] == str(output.tenant_id)
    assert payload["action"] == "approve"
    assert actions[0]["style"] == "positive"
    assert actions[1]["style"] == "destructive"


def test_empty_actions_list_omits_actions_field():
    card = render_agent_output(_output(actions=[]))["content"]
    assert "actions" not in card


def test_factset_caps_at_ten_entries():
    metadata = {f"k{i}": i for i in range(20)}
    card = render_agent_output(_output(metadata=metadata))["content"]
    facts = next(b for b in card["body"] if b["type"] == "FactSet")
    assert len(facts["facts"]) == 10
