"""Wire-format schemas exchanged with the Agent Runtime and Pipedream."""
from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CallbackAction(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    DISCARD = "discard"
    FEEDBACK = "feedback"


class AgentOutput(BaseModel):
    """Payload received on POST /deliver from the Agent Runtime."""

    task_id: UUID
    tenant_id: UUID
    rep_id: UUID
    slack_user_id: str = Field(description="External Slack user id resolved by the runtime.")
    dm_channel_id: str | None = Field(
        default=None,
        description="Optional pre-resolved DM channel id; falls back to conversations.open.",
    )
    title: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(
        default_factory=lambda: ["approve", "edit", "discard"],
        description="Which interactive buttons to show.",
    )


class ApprovalCallback(BaseModel):
    """Outbound to Agent Runtime /callbacks when a rep clicks a Slack button."""

    task_id: UUID
    rep_id: UUID
    action: CallbackAction
    edited_output: dict[str, Any] | None = None
    feedback: str | None = None


class FeedbackEvent(BaseModel):
    """Thumbs up/down feedback signal forwarded to the runtime."""

    task_id: UUID
    rep_id: UUID
    rating: int = Field(ge=-1, le=1, description="-1 for down, 1 for up, 0 for clear.")
    note: str | None = None


class OAuthToken(BaseModel):
    """Forwarded to the Pipedream oauth_relay workflow on completed OAuth flows."""

    tenant_id: UUID
    rep_id: UUID
    source: str
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    scopes: list[str] = Field(default_factory=list)
