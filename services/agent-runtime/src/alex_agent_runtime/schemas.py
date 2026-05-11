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


class ActionType(StrEnum):
    CRM_WRITE = "crm.write"
    EMAIL_SEND = "email.send"
    DOC_UPLOAD = "doc.upload"


class ActionRequest(BaseModel):
    """Approved external write the runtime asks Pipedream to dispatch."""

    action_id: str
    tenant_id: UUID
    rep_id: UUID
    approver_rep_id: UUID | None = None
    action_type: ActionType
    target_system: str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class DryRunRequest(BaseModel):
    """Validate a proposed CRM write without executing."""

    tenant_id: UUID
    rep_id: UUID
    action_type: ActionType
    target_system: str
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class DryRunResponse(BaseModel):
    valid: bool
    target_system: str
    target_id: str | None
    preview: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, str]] = Field(default_factory=list)


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


class OAuthToken(BaseModel):
    """Forwarded from the Messaging Surface OAuth redirect handler."""

    tenant_id: UUID
    rep_id: UUID
    source: str
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    scopes: list[str] = Field(default_factory=list)


class ConnectionStatusUpdate(BaseModel):
    """Posted by the OAuth relay workflow once a token has been vaulted."""

    tenant_id: UUID
    rep_id: UUID
    source: str
    status: ConnectionStatus
    scopes: list[str] = Field(default_factory=list)
    vault_ref: str | None = None


class ConnectionStatusView(BaseModel):
    """Read model returned by the ConnectionStatus query API."""

    tenant_id: UUID
    rep_id: UUID
    source: str
    status: ConnectionStatus
    scopes: list[str] = Field(default_factory=list)
    connected_at: datetime
    last_seen_at: datetime


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


# ---------------------------------------------------------------------------
# Persistent Memory (WO #7)
# ---------------------------------------------------------------------------
class MemoryTier(StrEnum):
    REP = "rep"
    DEAL = "deal"
    ACCOUNT = "account"
    ORG = "org"


class MemoryRecord(BaseModel):
    """Read model returned for individual memory rows."""

    id: UUID
    tier: MemoryTier
    tenant_id: UUID
    owner_id: UUID | None = Field(
        default=None,
        description=(
            "rep_id / deal_id / account_id depending on tier. Null for org-tier "
            "memories which scope to the tenant directly."
        ),
    )
    kind: str
    content: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_uri: str | None = None
    created_at: datetime
    updated_at: datetime


class MemoryWrite(BaseModel):
    """Input to MemoryStore writes."""

    tier: MemoryTier
    owner_id: UUID | None = None  # rep_id / deal_id / account_id; null for org
    kind: str
    content: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_uri: str | None = None


class MemoryContext(BaseModel):
    """Input to MemoryStore.retrieve().

    Identifies the scope (tenant + which rep / deal / account is in focus)
    and optionally a natural-language query for semantic retrieval. When
    ``query_text`` is set the retrieval layer runs an ANN search over
    each tier's embedding table and joins back to the structured rows;
    without a query it returns the most recent records per requested tier.
    """

    tenant_id: UUID
    rep_id: UUID | None = None
    deal_id: UUID | None = None
    account_id: UUID | None = None
    tiers: list[MemoryTier] = Field(
        default_factory=lambda: [
            MemoryTier.REP,
            MemoryTier.DEAL,
            MemoryTier.ACCOUNT,
            MemoryTier.ORG,
        ]
    )
    query_text: str | None = None
    kinds_filter: list[str] | None = None
    k_per_tier: int = Field(default=5, ge=1, le=50)


class MemorySnippet(BaseModel):
    """A single semantically-retrieved chunk with its parent memory row."""

    memory: MemoryRecord
    chunk_text: str
    similarity: float | None = None


class MemorySummary(BaseModel):
    """Output of MemoryStore.retrieve()."""

    tenant_id: UUID
    rep_id: UUID | None = None
    deal_id: UUID | None = None
    account_id: UUID | None = None
    by_tier: dict[MemoryTier, list[MemorySnippet]] = Field(default_factory=dict)


class EmbeddingChunk(BaseModel):
    text: str
    chunk_index: int
