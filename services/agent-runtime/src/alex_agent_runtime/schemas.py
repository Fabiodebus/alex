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

from pydantic import BaseModel, Field, model_validator


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


# ---------------------------------------------------------------------------
# WO #8 — MemorySummarizer + IngestionPipeline
# ---------------------------------------------------------------------------
class MemorySummaryUpdated(BaseModel):
    """Published by MemorySummarizer when a stored summary row is refreshed."""

    tenant_id: UUID
    tier: MemoryTier
    owner_id: UUID | None = None
    summary_memory_id: UUID
    sources_summarized: int


class IngestedRecordKind(StrEnum):
    CRM_OPPORTUNITY = "crm_opportunity"
    CRM_CONTACT = "crm_contact"
    EMAIL_THREAD = "email_thread"
    CALL_RECORDING = "call_recording"


class IngestedRecord(BaseModel):
    kind: IngestedRecordKind
    external_id: str
    content: str
    occurred_at: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class IngestionBatch(BaseModel):
    """What an IngestionProvider returns for a single rep's backfill."""

    tenant_id: UUID
    rep_id: UUID
    fetched_at: datetime
    records: list[IngestedRecord] = Field(default_factory=list)


class IngestionResult(BaseModel):
    tenant_id: UUID
    rep_id: UUID
    records_processed: int
    memories_written: int
    memories_deduplicated: int
    started_at: datetime
    completed_at: datetime
    errors: list[str] = Field(default_factory=list)


class IngestionComplete(BaseModel):
    """Published by IngestionPipeline once a backfill finishes."""

    tenant_id: UUID
    rep_id: UUID
    result: IngestionResult


class IngestionStartRequest(BaseModel):
    """Body for POST /ingestion/start."""

    tenant_id: UUID
    rep_id: UUID
    since_days: int = Field(default=90, ge=1, le=365)


# ---------------------------------------------------------------------------
# WO #9 — CRM Integration: CRMReader
# ---------------------------------------------------------------------------
class CRMPlatform(StrEnum):
    HUBSPOT = "hubspot"
    SALESFORCE = "salesforce"
    PIPEDRIVE = "pipedrive"
    CLOSE = "close"


class CRMRecordKind(StrEnum):
    OPPORTUNITY = "opportunity"
    CONTACT = "contact"
    ACCOUNT = "account"


class CRMStakeholder(BaseModel):
    """A single contact attached to an opportunity or account."""

    external_id: str | None = None
    name: str | None = None
    email: str | None = None
    title: str | None = None
    role: str | None = None  # 'economic_buyer' | 'champion' | 'influencer' | ...


class CRMRecord(BaseModel):
    """Canonical CRM record consumed by every feature workflow.

    Feature logic never sees the source CRM's field names — adapters
    fold platform-specific shapes into this contract. Fields are
    intentionally permissive (most are optional) so a record that only
    exposes a subset still round-trips cleanly.
    """

    platform: CRMPlatform
    kind: CRMRecordKind
    external_id: str
    name: str | None = None

    # Opportunity-specific
    stage: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    probability: float | None = Field(default=None, ge=0.0, le=1.0)
    close_date: datetime | None = None
    owner_email: str | None = None
    account_external_id: str | None = None
    stakeholders: list[CRMStakeholder] = Field(default_factory=list)

    # Contact-specific
    email: str | None = None
    title: str | None = None
    phone: str | None = None

    # Account-specific
    domain: str | None = None
    industry: str | None = None
    country: str | None = None

    # MEDDIC/MEDDPICC fields — populated when the org's CRM mapping
    # surfaces them. Each entry is a free-text capture from the CRM
    # rather than an enforced enum (different orgs use different
    # vocabularies for the same letter).
    meddic: dict[str, str] = Field(default_factory=dict)

    updated_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CRMFetchRequest(BaseModel):
    """Sent to the Pipedream `crm_fetch` workflow when MemoryStore misses."""

    tenant_id: UUID
    platform: CRMPlatform
    kind: CRMRecordKind
    external_id: str


class CRMSyncResult(BaseModel):
    """Returned by CRMReader.handle for inbound CRMDataSync events."""

    platform: CRMPlatform
    kind: CRMRecordKind
    external_id: str
    cached: bool  # True iff the record was written/refreshed in MemoryStore
    deduplicated: bool  # True iff the record's content hash already existed


# ---------------------------------------------------------------------------
# WO #10 — CRM Integration: CRMWriter & CRMValidator
# ---------------------------------------------------------------------------
class FieldUpdate(BaseModel):
    """A single field-level write proposal.

    The blueprint's safety rule is enforced here at the type level: every
    update MUST carry ``current_value`` alongside ``proposed_value`` so the
    approval payload can display both. ``None`` is a legal current value
    (the field is empty today), but the field itself must be present.
    """

    platform: CRMPlatform
    kind: CRMRecordKind
    external_id: str
    field_name: str = Field(min_length=1)
    current_value: Any = Field(
        default=None,
        description=(
            "What the CRM holds today. Caller (the feature workflow) is "
            "responsible for populating this from a fresh CRMReader read."
        ),
    )
    proposed_value: Any = None

    # Distinguishes 'caller didn't set current_value at all' (rejected by
    # CRMValidator) from 'caller explicitly set it to None / null'. Set
    # automatically from `model_fields_set` so callers can't bypass the
    # safety rule by omitting the field.
    has_current_value: bool = True

    @model_validator(mode="after")
    def _capture_current_value_presence(self) -> "FieldUpdate":
        if "current_value" not in self.model_fields_set and "has_current_value" not in self.model_fields_set:
            object.__setattr__(self, "has_current_value", False)
        return self


class CRMNote(BaseModel):
    """A free-text note attached to an opportunity/contact/account.

    Notes are append-only on the CRM side so they bypass the
    'current_value' rule, but they still flow through CRMWriter so they
    are audit-logged.
    """

    platform: CRMPlatform
    kind: CRMRecordKind
    external_id: str
    body: str = Field(min_length=1)
    title: str | None = None


class ValidatedFieldUpdate(BaseModel):
    """A FieldUpdate that has cleared CRMValidator.

    Carries the original update plus a ``normalized_value`` produced by
    the platform-specific validator (e.g. enum lookups resolved to the
    platform's canonical option_id, currency strings uppercased)."""

    update: FieldUpdate
    normalized_value: Any
    platform_field_id: str | None = Field(
        default=None,
        description="Platform-specific field identifier when distinct from field_name.",
    )


class CRMValidationError(BaseModel):
    """Structured rejection from CRMValidator."""

    code: str
    message: str
    field_name: str | None = None


class CRMValidationResult(BaseModel):
    """Validator output for a single FieldUpdate."""

    validated: ValidatedFieldUpdate | None = None
    error: CRMValidationError | None = None

    @property
    def is_valid(self) -> bool:
        return self.validated is not None and self.error is None


class DryRunCRMRequest(BaseModel):
    """A batch of proposed updates the caller wants to preview.

    Distinct from the older ``DryRunRequest`` (which targets the existing
    Pipedream ``dry_run_crm_write`` action) because this batch operates
    entirely inside the runtime — no Pipedream round-trip — and so feature
    workflows can build the rep-facing diff without leaving the process.
    """

    tenant_id: UUID
    rep_id: UUID
    updates: list[FieldUpdate] = Field(default_factory=list)
    notes: list[CRMNote] = Field(default_factory=list)


class DryRunCRMResult(BaseModel):
    """Resolution of a ``DryRunCRMRequest``.

    Carries enough detail for the messaging surface to render the "before
    -> after" approval card without re-running validation.
    """

    valid: bool
    validated_updates: list[ValidatedFieldUpdate] = Field(default_factory=list)
    errors: list[CRMValidationError] = Field(default_factory=list)
    notes: list[CRMNote] = Field(default_factory=list)


class CRMWriteStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"  # validator-side reject; never dispatched


class CRMWriteRequest(BaseModel):
    """What CRMWriter sends downstream (Pipedream) once a write is approved.

    Mirrors the ``ActionRequest`` shape so the existing dispatch path can
    consume it, but typed for the CRM-write subset so the wire contract
    is enforced by Pydantic rather than by convention."""

    tenant_id: UUID
    rep_id: UUID
    approver_rep_id: UUID | None = None
    platform: CRMPlatform
    kind: CRMRecordKind
    external_id: str
    field_updates: list[ValidatedFieldUpdate] = Field(default_factory=list)
    notes: list[CRMNote] = Field(default_factory=list)
    idempotency_key: str = Field(
        description="Caller-provided dedup key — survives retries on the Pipedream side.",
    )


class CRMWriteResult(BaseModel):
    """What CRMWriter returns after dispatching."""

    status: CRMWriteStatus
    platform: CRMPlatform
    external_id: str
    succeeded_fields: list[str] = Field(default_factory=list)
    failed_fields: list[str] = Field(default_factory=list)
    error: CRMValidationError | None = None
    audit_log_id: UUID | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)


class CRMWriteFailed(BaseModel):
    """Published on the EventBus when a dispatched write fails.

    Notification Delivery (WO #13) subscribes and routes a plain-language
    message to the rep. Carried fields are the minimum needed to render
    that message without re-querying the CRM.
    """

    tenant_id: UUID
    rep_id: UUID
    platform: CRMPlatform
    external_id: str
    field_names: list[str] = Field(default_factory=list)
    reason: str
    audit_log_id: UUID | None = None


# ---------------------------------------------------------------------------
# WO #11 — Calendar & Meeting Detection
# ---------------------------------------------------------------------------
class CalendarProvider(StrEnum):
    GOOGLE = "google_calendar"
    OUTLOOK = "outlook_calendar"


class CalendarEventStatus(StrEnum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


class CalendarAttendee(BaseModel):
    email: str
    name: str | None = None
    response_status: str | None = Field(
        default=None,
        description="provider-native response state (accepted/declined/tentative/needsAction)",
    )
    is_organizer: bool = False


class CalendarEvent(BaseModel):
    """Canonical calendar payload produced by the Pipedream side.

    Both Google Calendar and Outlook Calendar are folded into this
    shape upstream so :class:`MeetingClassifier` operates on a single
    schema."""

    provider: CalendarProvider
    calendar_event_id: str = Field(min_length=1)
    tenant_id: UUID
    rep_id: UUID
    rep_email: str
    title: str | None = None
    description: str | None = None
    location: str | None = None
    start_at: datetime
    end_at: datetime
    status: CalendarEventStatus = CalendarEventStatus.CONFIRMED
    organizer_email: str | None = None
    attendees: list[CalendarAttendee] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class AttendeeProfile(BaseModel):
    """An attendee after CRM resolution + classification.

    ``is_external`` is True iff the attendee's email domain differs from
    the rep's. ``crm_contact_external_id`` and ``crm_account_external_id``
    are populated when MeetingClassifier successfully joins to a cached
    CRM contact/account; ``None`` when no match was found.
    """

    email: str
    name: str | None = None
    is_external: bool
    response_status: str | None = None
    is_organizer: bool = False
    crm_contact_external_id: str | None = None
    crm_account_external_id: str | None = None


class MeetingDetected(BaseModel):
    """Published when a CalendarEvent has been classified and resolved.

    Per the blueprint, ``opportunity_external_id`` is null when no CRM
    match was found — downstream features are responsible for handling
    the no-opportunity case rather than suppressing output."""

    tenant_id: UUID
    rep_id: UUID
    calendar_event_id: str
    provider: CalendarProvider
    start_at: datetime
    end_at: datetime
    trigger_at: datetime = Field(
        description="When downstream features (e.g. Meeting Prep) should fire.",
    )
    title: str | None = None
    is_external: bool
    attendee_profiles: list[AttendeeProfile] = Field(default_factory=list)
    opportunity_external_id: str | None = None
    account_external_id: str | None = None
    crm_platform: CRMPlatform | None = None


class MeetingCompleted(BaseModel):
    tenant_id: UUID
    rep_id: UUID
    calendar_event_id: str
    provider: CalendarProvider
    start_at: datetime
    end_at: datetime
    title: str | None = None
    opportunity_external_id: str | None = None
    account_external_id: str | None = None
    crm_platform: CRMPlatform | None = None


class MeetingCancelled(BaseModel):
    tenant_id: UUID
    rep_id: UUID
    calendar_event_id: str
    provider: CalendarProvider
    title: str | None = None
    opportunity_external_id: str | None = None


class CalendarLifecycleState(StrEnum):
    """Internal book-keeping written into the memory row's attributes so
    the completion-scan job can tell which events are still pending."""

    DETECTED = "detected"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# WO #12 — Approval Workflow
# ---------------------------------------------------------------------------
class PendingTaskStatus(StrEnum):
    """Subset of task_state.status that this WO operates on."""

    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ApprovalOutcome(StrEnum):
    """Terminal outcomes recorded by ApprovalHandler / ApprovalExpiryScan."""

    APPROVED = "approved"
    EDITED = "edited"
    DISCARDED = "discarded"
    EXPIRED = "expired"


class PendingTaskCreate(BaseModel):
    """Input to :meth:`ApprovalGate.create_pending_task`.

    The ``task_type`` is the dotted action kind the rep is being asked
    to approve (``crm.write``, ``email.send``, …). ``payload`` carries
    the full proposed action so the audit log + downstream dispatcher
    can replay it once approved.
    """

    tenant_id: UUID
    rep_id: UUID
    task_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    expires_in_hours: int = Field(default=24, ge=1, le=24 * 14)
    parent_task_id: UUID | None = None


class PendingTask(BaseModel):
    """Read model returned by :class:`ApprovalGate`."""

    task_id: UUID
    tenant_id: UUID
    assignee_rep_id: UUID
    task_type: str
    status: PendingTaskStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    deadline: datetime
    created_at: datetime
    updated_at: datetime


class ApprovalRequested(BaseModel):
    """Published by ApprovalGate after a PendingTask lands; consumed by
    Notification Delivery (WO #13) to fan out an approval card."""

    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    task_type: str
    title: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    deadline: datetime


class EditDiff(BaseModel):
    """Captured when a rep approves with edits.

    VoiceUpdater (WO #14, Voice Model) subscribes so the rep's editing
    patterns feed the per-rep voice training loop.
    """

    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    task_type: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class TaskApproved(BaseModel):
    """Published on ``approval.approved`` after the audit row is written.

    The ApprovedActionDispatcher subscribes and routes by ``task_type``
    to the concrete executor (CRMWriter, future EmailDispatcher, …)."""

    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    task_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    edit_diff: EditDiff | None = None


class TaskDiscarded(BaseModel):
    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    task_type: str
    feedback: str | None = None


class TaskExpired(BaseModel):
    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    task_type: str
    deadline: datetime


# ---------------------------------------------------------------------------
# WO #13 — Notification Delivery
# ---------------------------------------------------------------------------
class DeliveryChannel(StrEnum):
    SLACK = "slack"
    TEAMS = "teams"
    CRM_NATIVE = "crm_native"


class OutputType(StrEnum):
    """Coarse categories used as the lookup key for DeliveryPreference.

    Feature WOs may add more types; the OutputRouter falls back to
    SLACK whenever a type has no explicit preference."""

    APPROVAL_REQUEST = "approval_request"
    NOTIFICATION = "notification"
    DAILY_BRIEF = "daily_brief"
    MEETING_PREP = "meeting_prep"
    CRM_WRITE_FAILED = "crm_write_failed"


class DeliveryStatusValue(StrEnum):
    """Mirrors the DB check constraint on delivery_statuses.status."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    ESCALATED = "escalated"


class DeliveryRequest(BaseModel):
    """Input to :meth:`OutputRouter.deliver`.

    The router uses ``output_id`` as the per-tenant idempotency key —
    re-delivering the same output_id flips the existing row rather
    than creating a new one. Callers that re-send the same logical
    output (e.g. on a retry) should keep the id stable.
    """

    tenant_id: UUID
    rep_id: UUID
    output_id: str = Field(min_length=1)
    output_type: OutputType | str
    task_id: UUID | None = None
    title: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryStatus(BaseModel):
    """Read model returned by :class:`DeliveryTracker`."""

    id: UUID
    tenant_id: UUID
    rep_id: UUID
    task_id: UUID | None = None
    output_id: str
    output_type: str
    channel: DeliveryChannel
    status: DeliveryStatusValue
    attempt_count: int
    last_attempt_at: datetime | None = None
    acknowledged_at: datetime | None = None
    escalated_at: datetime | None = None
    retry_after: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class DeliveryAttempt(BaseModel):
    """Wire payload handed to a :class:`MessagingDeliveryClient`.

    Concrete clients (Slack, Teams) translate this into their surface-
    specific call. ``rep_id`` lets the surface side resolve the
    messaging identity (e.g. Slack ``slack_user_id``)."""

    tenant_id: UUID
    rep_id: UUID
    output_id: str
    output_type: str
    task_id: UUID | None = None
    title: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryEscalated(BaseModel):
    """Published when a delivery's retry window has elapsed.

    Daily Brief (a future WO) subscribes and surfaces the unactioned
    output to the rep on the next brief assembly."""

    tenant_id: UUID
    rep_id: UUID
    output_id: str
    output_type: str
    channel: DeliveryChannel
    attempt_count: int
    escalated_at: datetime


# ---------------------------------------------------------------------------
# WO #14 — Voice Model
# ---------------------------------------------------------------------------
class VoiceLanguage(StrEnum):
    """Languages tracked separately in the voice profile.

    Only English and German are supported in v1 per the blueprint.
    Other locales fall back to English."""

    EN = "en"
    DE = "de"


class GermanRegister(StrEnum):
    """German Sie/Du register selection. ``MIXED`` is the safe default
    while the model accumulates signal; the applicator escalates to a
    concrete value once observed often enough."""

    SIE = "sie"
    DU = "du"
    MIXED = "mixed"


class VoicePatternStat(BaseModel):
    """One weighted phrase pattern (greeting, sign-off, signature)."""

    phrase: str = Field(min_length=1)
    weight: float = Field(default=0.0, ge=0.0, le=1.0)


class VoiceLanguageProfile(BaseModel):
    """Per-language sub-profile inside a :class:`VoiceProfile`."""

    greetings: list[VoicePatternStat] = Field(default_factory=list)
    signoffs: list[VoicePatternStat] = Field(default_factory=list)
    signature_phrases: list[VoicePatternStat] = Field(default_factory=list)
    forbidden_phrases: list[VoicePatternStat] = Field(default_factory=list)
    # Tone dimensions in [0.0, 1.0] — 0.5 is the neutral default.
    formality: float = Field(default=0.5, ge=0.0, le=1.0)
    warmth: float = Field(default=0.5, ge=0.0, le=1.0)
    directness: float = Field(default=0.5, ge=0.0, le=1.0)
    brevity: float = Field(default=0.5, ge=0.0, le=1.0)
    # German-only attribute; ignored in the EN sub-profile.
    de_register: GermanRegister = GermanRegister.MIXED
    sample_count: int = 0


class VoiceProfile(BaseModel):
    """Per-rep voice representation stored in REP-tier memory.

    Version monotonically increases on every update. The most recent
    row is the active profile; earlier versions are the revert history.
    """

    rep_id: UUID
    version: int = 1
    languages: dict[VoiceLanguage, VoiceLanguageProfile] = Field(
        default_factory=lambda: {
            VoiceLanguage.EN: VoiceLanguageProfile(),
            VoiceLanguage.DE: VoiceLanguageProfile(),
        }
    )
    updated_at: datetime | None = None
    notes: str | None = None  # short human-readable rationale on revert


class VoiceSignal(BaseModel):
    """Output of :class:`VoiceSignalExtractor.extract`.

    Carries per-language deltas the updater applies to the active
    profile. Empty lists mean no signal for that field."""

    language: VoiceLanguage = VoiceLanguage.EN
    added_greetings: list[str] = Field(default_factory=list)
    removed_greetings: list[str] = Field(default_factory=list)
    added_signoffs: list[str] = Field(default_factory=list)
    removed_signoffs: list[str] = Field(default_factory=list)
    added_phrases: list[str] = Field(default_factory=list)
    removed_phrases: list[str] = Field(default_factory=list)
    # Length delta as a ratio of the original length, clamped to [-1, 1].
    # Positive means the rep wrote a longer draft than Alex; negative
    # means the rep trimmed.
    length_delta_ratio: float = 0.0
    de_register_signal: GermanRegister | None = None


class VoiceApplication(BaseModel):
    """Metadata tag attached to each generated draft.

    Used to correlate drafts with the profile version that produced
    them — important for the "revert when a version regresses" path
    in the blueprint."""

    rep_id: UUID
    profile_version: int
    language: VoiceLanguage
    de_register: GermanRegister | None = None
    account_external_id: str | None = None
    applied_at: datetime


class DiscardSignal(BaseModel):
    """Published when a rep discards a draft.

    The updater records the discarded text under
    ``forbidden_phrases`` (negatively weighted) without ever resetting
    the profile."""

    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    language: VoiceLanguage = VoiceLanguage.EN
    discarded_body: str
    feedback: str | None = None


# ---------------------------------------------------------------------------
# WO #15 / #16 — Onboarding (Conversational Flow, OAuth, Activation)
# ---------------------------------------------------------------------------
class OnboardingConnector(StrEnum):
    """Connectors offered in the v1 onboarding sequence.

    Slack is the messaging surface itself, not a connector here. The
    connector list mirrors the user-chosen v1 set: Close (CRM),
    Google (email + calendar paired into a single OAuth step), and
    Krisp.ai (recording, optional)."""

    CLOSE = "close"
    GOOGLE = "google"
    KRISP = "krisp"


class OnboardingStep(StrEnum):
    WELCOME = "welcome"
    CONNECT_CLOSE = "connect_close"
    CONNECT_GOOGLE = "connect_google"
    CONNECT_KRISP = "connect_krisp"
    INGESTING = "ingesting"
    AWAITING_FIRST_OUTPUT = "awaiting_first_output"
    COMPLETED = "completed"
    FAILED = "failed"


class ConnectorConnectionStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    CONNECTED = "connected"
    SKIPPED = "skipped"
    FAILED = "failed"


class ConnectorStatus(BaseModel):
    """Per-connector slice of OnboardingState.connector_status."""

    status: ConnectorConnectionStatus = ConnectorConnectionStatus.NOT_STARTED
    attempted_at: datetime | None = None
    connected_at: datetime | None = None
    token_ref: str | None = None
    failure_reason: str | None = None


class OnboardingState(BaseModel):
    """Read model returned by :class:`OnboardingStateRepo`."""

    id: UUID
    tenant_id: UUID
    rep_id: UUID
    current_step: OnboardingStep
    completed_steps: list[OnboardingStep] = Field(default_factory=list)
    connector_status: dict[OnboardingConnector, ConnectorStatus] = Field(
        default_factory=dict
    )
    started_at: datetime | None = None
    ingestion_complete_at: datetime | None = None
    first_proactive_at: datetime | None = None
    activation_milestone_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OAuthInitiation(BaseModel):
    """Returned by :meth:`OAuthOrchestrator.initiate`."""

    connector: OnboardingConnector
    state: str = Field(min_length=8, description="CSRF + correlation token")
    authorize_url: str
    stub: bool = False
    expires_at: datetime


class OAuthCompletion(BaseModel):
    """Outcome of :meth:`OAuthOrchestrator.handle_callback`."""

    connector: OnboardingConnector
    success: bool
    token_ref: str | None = None
    failure_reason: str | None = None
    rep_id: UUID
    tenant_id: UUID


class OnboardingMessage(BaseModel):
    """Block-Kit-friendly payload OutputRouter ships to the Slack bot."""

    rep_id: UUID
    tenant_id: UUID
    step: OnboardingStep
    title: str
    body: str
    actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of {action_id, label, value, style?} entries the bot renders as buttons.",
    )


class OnboardingProgress(BaseModel):
    """Published on the EventBus when onboarding state advances."""

    tenant_id: UUID
    rep_id: UUID
    step: OnboardingStep
    completed_connectors: list[OnboardingConnector] = Field(default_factory=list)


class FirstProactiveOutputType(StrEnum):
    """The blueprint's three priority-ordered first-output categories."""

    MEETING_PREP = "meeting_prep"
    FOLLOW_UP_DRAFT = "follow_up_draft"
    STALLED_DEAL_SUMMARY = "stalled_deal_summary"
    FALLBACK_INTRO = "fallback_intro"


class FirstProactiveSelection(BaseModel):
    """What :class:`ActivationTracker` picked + why."""

    tenant_id: UUID
    rep_id: UUID
    output_type: FirstProactiveOutputType
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ActivationMilestone(BaseModel):
    """Published when the rep approves their first agent-generated draft."""

    tenant_id: UUID
    rep_id: UUID
    task_id: UUID
    achieved_at: datetime
