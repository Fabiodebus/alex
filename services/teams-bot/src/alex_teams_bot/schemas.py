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
    """Payload received on POST /deliver from the Agent Runtime.

    The Agent Runtime is responsible for resolving the rep's saved
    Bot Framework `ConversationReference` (typically captured on first
    interaction and persisted in `messaging_identities`) and inlining it
    here so this service can stay stateless.
    """

    task_id: UUID
    tenant_id: UUID
    rep_id: UUID
    conversation_reference: dict[str, Any] = Field(
        description="Serialized Bot Framework ConversationReference for the rep's DM."
    )
    title: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=lambda: ["approve", "edit", "discard"])


class ApprovalCallback(BaseModel):
    task_id: UUID
    rep_id: UUID
    action: CallbackAction
    edited_output: dict[str, Any] | None = None
    feedback: str | None = None


class FeedbackEvent(BaseModel):
    task_id: UUID
    rep_id: UUID
    rating: int = Field(ge=-1, le=1)
    note: str | None = None


class OAuthToken(BaseModel):
    tenant_id: UUID
    rep_id: UUID
    source: str
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    scopes: list[str] = Field(default_factory=list)
