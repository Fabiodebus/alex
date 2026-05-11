"""AgentOutput → Block Kit renderer.

Block Kit blocks are stable JSON; we keep the renderer pure so it's easy
to snapshot-test and so a future Teams renderer can compose the same
content into Adaptive Cards from the same `AgentOutput` source.
"""
from __future__ import annotations

import json
from typing import Any

from ..schemas import AgentOutput

ACTION_LABELS: dict[str, str] = {
    "approve": "Approve",
    "edit": "Edit",
    "discard": "Discard",
    "feedback": "Send feedback",
}

ACTION_STYLES: dict[str, str | None] = {
    "approve": "primary",
    "edit": None,
    "discard": "danger",
    "feedback": None,
}


def render_agent_output(output: AgentOutput) -> list[dict[str, Any]]:
    """Return Block Kit blocks for a single AgentOutput."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": output.title[:150], "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output.body},
        },
    ]
    if output.metadata:
        blocks.append({"type": "context", "elements": _context_elements(output.metadata)})

    if output.actions:
        blocks.append(
            {
                "type": "actions",
                "block_id": "alex_actions",
                "elements": [
                    _button(action=action, task_id=str(output.task_id), rep_id=str(output.rep_id))
                    for action in output.actions
                ],
            }
        )
    return blocks


def _button(*, action: str, task_id: str, rep_id: str) -> dict[str, Any]:
    label = ACTION_LABELS.get(action, action.title())
    button: dict[str, Any] = {
        "type": "button",
        "action_id": f"alex.{action}",
        "text": {"type": "plain_text", "text": label, "emoji": True},
        "value": json.dumps({"task_id": task_id, "rep_id": rep_id, "action": action}),
    }
    style = ACTION_STYLES.get(action)
    if style is not None:
        button["style"] = style
    return button


def _context_elements(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for key, value in metadata.items():
        if not isinstance(value, (str, int, float, bool)) or value is None:
            continue
        elements.append({"type": "mrkdwn", "text": f"*{key}:* {value}"})
        if len(elements) == 10:  # Block Kit caps context elements
            break
    if not elements:
        elements.append({"type": "mrkdwn", "text": "_no metadata_"})
    return elements
