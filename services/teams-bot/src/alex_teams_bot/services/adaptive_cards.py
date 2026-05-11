"""AgentOutput → Adaptive Card renderer.

Pure function so the renderer is snapshot-testable and so the underlying
``AgentOutput`` content can be reused identically with the slack-bot's
Block Kit renderer. Card schema v1.5; Action.Submit carries the same
``{task_id, rep_id, action, tenant_id?}`` JSON blob as the slack button
``value`` so the Agent Runtime can route both surfaces uniformly.
"""
from __future__ import annotations

from typing import Any

from ..schemas import AgentOutput

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

ACTION_LABELS: dict[str, str] = {
    "approve": "Approve",
    "edit": "Edit",
    "discard": "Discard",
    "feedback": "Send feedback",
}

ACTION_STYLES: dict[str, str] = {
    "approve": "positive",
    "edit": "default",
    "discard": "destructive",
    "feedback": "default",
}


def render_agent_output(output: AgentOutput) -> dict[str, Any]:
    """Return a Bot Framework Attachment dict wrapping an Adaptive Card v1.5."""
    body_blocks: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": output.title[:200],
            "weight": "Bolder",
            "size": "Large",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": output.body,
            "wrap": True,
            "spacing": "Medium",
        },
    ]
    if output.metadata:
        body_blocks.append(_facts_block(output.metadata))

    actions = [_action(name=action, output=output) for action in output.actions]

    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body_blocks,
    }
    if actions:
        card["actions"] = actions

    return {
        "contentType": ADAPTIVE_CARD_CONTENT_TYPE,
        "content": card,
    }


def _facts_block(metadata: dict[str, Any]) -> dict[str, Any]:
    facts: list[dict[str, str]] = []
    for key, value in metadata.items():
        if not isinstance(value, (str, int, float, bool)) or value is None:
            continue
        facts.append({"title": str(key), "value": str(value)})
        if len(facts) == 10:
            break
    if not facts:
        facts.append({"title": " ", "value": "no metadata"})
    return {"type": "FactSet", "facts": facts, "spacing": "Medium"}


def _action(*, name: str, output: AgentOutput) -> dict[str, Any]:
    payload = {
        "task_id": str(output.task_id),
        "rep_id": str(output.rep_id),
        "tenant_id": str(output.tenant_id),
        "action": name,
    }
    title = ACTION_LABELS.get(name, name.title())
    action: dict[str, Any] = {
        "type": "Action.Submit",
        "id": f"alex.{name}",
        "title": title,
        "data": {"alex": payload},
    }
    style = ACTION_STYLES.get(name)
    if style is not None:
        action["style"] = style
    return action
