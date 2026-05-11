"""Wire-format Pydantic schemas exchanged with the messaging surface and
Pipedream Integration Layer.

These models are deliberately permissive on payload shape — feature WOs
will tighten them as they implement specific event kinds.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    """Coarse routing taxonomy. Feature WOs add finer-grained kinds."""

    CALENDAR_MEETING_DETECTED = "calendar.meeting_detected"
    RECORDING_COMPLETED = "recording.completed"
    CRM_ACTIVITY_LOGGED = "crm.activity_logged"
    DEAL_INACTIVITY_DETECTED = "deal.inactivity_detected"
    EMAIL_RECEIVED = "email.received"
    UNKNOWN = "unknown"


class IntegrationEvent(BaseModel):
    """Normalized inbound event from the Pipedream Integration Layer."""

    event_id: str = Field(min_length=1, description="Pipedream-supplied unique id; dedupe key.")
    source: str = Field(min_length=1, description="Origin system (e.g., 'gong', 'hubspot').")
    kind: EventKind | str
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class CallbackAction(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    DISCARD = "discard"
    FEEDBACK = "feedback"


class ApprovalCallback(BaseModel):
    """Inbound rep decision from Slack/Teams."""

    task_id: UUID
    rep_id: UUID
    action: CallbackAction
    edited_output: dict[str, Any] | None = None
    feedback: str | None = None


class AgentResponse(BaseModel):
    """Result returned by an `AgentBackend.run(...)` call."""

    text: str
    raw: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    backend: str = "stub"


class ActionRequest(BaseModel):
    """Approved external write the runtime asks Pipedream to dispatch."""

    action_type: str
    target_system: str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    """Structured output the runtime hands the messaging surface."""

    task_id: UUID
    title: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditLogEntry(BaseModel):
    """Append-only audit log row written before any approved external action."""

    action_type: str
    actor_rep_id: UUID | None = None
    approver_rep_id: UUID | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    prompt: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
