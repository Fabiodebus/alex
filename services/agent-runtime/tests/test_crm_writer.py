"""Integration tests for CRMWriter (hits Postgres for the audit row)."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from alex_agent_runtime.db import admin_session
from alex_agent_runtime.schemas import (
    CRMNote,
    CRMPlatform,
    CRMRecordKind,
    CRMWriteFailed,
    CRMWriteRequest,
    CRMWriteResult,
    CRMWriteStatus,
    FieldUpdate,
    ValidatedFieldUpdate,
)
from alex_agent_runtime.services.crm_validator import CRMValidator
from alex_agent_runtime.services.crm_write_client import CRMWriteError
from alex_agent_runtime.services.crm_writer import CRMWriter, CRMWriterError
from alex_agent_runtime.services.event_bus import EventBus
from alex_agent_runtime.tenant_context import tenant_scope


def _validated_stage_update(*, proposed: str = "Presentation") -> ValidatedFieldUpdate:
    """Drive a real FieldUpdate through CRMValidator so the test exercises
    the same shape the runtime produces in prod."""
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-write-1",
        field_name="dealstage",
        current_value="qualification",
        proposed_value=proposed,
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid and result.validated is not None
    return result.validated


class _RecordingWriteClient:
    """Test stub that records calls and produces a deterministic result."""

    name = "test-recording"

    def __init__(self) -> None:
        self.calls: list[CRMWriteRequest] = []
        self._next: CRMWriteResult | Exception | None = None

    def stage_success(self, *, succeeded_fields: list[str] | None = None) -> None:
        self._next = CRMWriteResult(
            status=CRMWriteStatus.SUCCEEDED,
            platform=CRMPlatform.HUBSPOT,
            external_id="deal-write-1",
            succeeded_fields=succeeded_fields or ["dealstage"],
        )

    def stage_partial_failure(self) -> None:
        self._next = CRMWriteResult(
            status=CRMWriteStatus.FAILED,
            platform=CRMPlatform.HUBSPOT,
            external_id="deal-write-1",
            succeeded_fields=[],
            failed_fields=["dealstage"],
            raw_response={"error": "field locked by another integration"},
        )

    def stage_transport_error(self) -> None:
        self._next = CRMWriteError("upstream blew up", status=502)

    async def write(self, request: CRMWriteRequest) -> CRMWriteResult:
        self.calls.append(request)
        nxt = self._next
        if isinstance(nxt, Exception):
            raise nxt
        assert nxt is not None, "test forgot to stage a response"
        return nxt


@pytest.mark.asyncio
async def test_execute_records_audit_row_and_returns_audit_id(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    client.stage_success()
    bus = EventBus()
    seen: list[CRMWriteFailed] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe("crm.write_failed", listener)
    writer = CRMWriter(write_client=client, event_bus=bus)

    with tenant_scope(tenant):
        result = await writer.execute(
            tenant_id=tenant,
            rep_id=rep,
            approver_rep_id=rep,
            platform=CRMPlatform.HUBSPOT,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="deal-write-1",
            field_updates=[_validated_stage_update()],
        )

    assert result.status is CRMWriteStatus.SUCCEEDED
    assert result.succeeded_fields == ["dealstage"]
    assert result.audit_log_id is not None
    assert client.calls and client.calls[0].field_updates[0].update.field_name == "dealstage"
    # Successful writes don't publish to crm.write_failed.
    assert seen == []

    # Inspect the audit row directly. Use admin_session so the assertion
    # is not subject to RLS surprises mid-test.
    async with admin_session() as session:
        row = await session.execute(
            text(
                "SELECT action_type, target_type, prompt, output, metadata "
                "FROM audit_log WHERE id = :id"
            ),
            {"id": str(result.audit_log_id)},
        )
        record = row.mappings().one()
    assert record["action_type"] == "crm.write"
    assert record["target_type"] == "crm_record"
    assert record["prompt"]["platform"] == "hubspot"
    assert record["prompt"]["field_updates"][0]["before"] == "qualification"
    assert record["prompt"]["field_updates"][0]["after_normalized"] == "presentation"
    assert record["output"]["status"] == "succeeded"
    assert record["output"]["succeeded_fields"] == ["dealstage"]
    assert record["metadata"]["platform"] == "hubspot"


@pytest.mark.asyncio
async def test_execute_publishes_failure_event_on_partial_failure(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    client.stage_partial_failure()
    bus = EventBus()
    seen: list[CRMWriteFailed] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe("crm.write_failed", listener)
    writer = CRMWriter(write_client=client, event_bus=bus)

    with tenant_scope(tenant):
        result = await writer.execute(
            tenant_id=tenant,
            rep_id=rep,
            approver_rep_id=rep,
            platform=CRMPlatform.HUBSPOT,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="deal-write-1",
            field_updates=[_validated_stage_update()],
        )

    assert result.status is CRMWriteStatus.FAILED
    assert result.failed_fields == ["dealstage"]
    assert seen and isinstance(seen[0], CRMWriteFailed)
    assert seen[0].field_names == ["dealstage"]
    assert "field locked" in seen[0].reason
    assert seen[0].audit_log_id == result.audit_log_id


@pytest.mark.asyncio
async def test_execute_handles_transport_error_without_raising(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    client.stage_transport_error()
    bus = EventBus()
    seen: list[CRMWriteFailed] = []

    async def listener(payload):
        seen.append(payload)

    bus.subscribe("crm.write_failed", listener)
    writer = CRMWriter(write_client=client, event_bus=bus)

    with tenant_scope(tenant):
        result = await writer.execute(
            tenant_id=tenant,
            rep_id=rep,
            approver_rep_id=rep,
            platform=CRMPlatform.HUBSPOT,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="deal-write-1",
            field_updates=[_validated_stage_update()],
        )

    assert result.status is CRMWriteStatus.FAILED
    assert result.raw_response.get("status") == 502
    assert seen and "upstream blew up" in seen[0].reason


@pytest.mark.asyncio
async def test_execute_rejects_empty_batch(tenant: UUID, rep: UUID):
    writer = CRMWriter(write_client=_RecordingWriteClient())
    with tenant_scope(tenant):
        with pytest.raises(CRMWriterError):
            await writer.execute(
                tenant_id=tenant,
                rep_id=rep,
                approver_rep_id=rep,
                platform=CRMPlatform.HUBSPOT,
                kind=CRMRecordKind.OPPORTUNITY,
                external_id="deal-write-1",
                field_updates=[],
                notes=[],
            )


@pytest.mark.asyncio
async def test_execute_accepts_notes_only_batch(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    client.stage_success(succeeded_fields=[])
    writer = CRMWriter(write_client=client)
    note = CRMNote(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-write-1",
        body="Customer asked for SLA addendum.",
    )

    with tenant_scope(tenant):
        result = await writer.execute(
            tenant_id=tenant,
            rep_id=rep,
            approver_rep_id=rep,
            platform=CRMPlatform.HUBSPOT,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="deal-write-1",
            field_updates=[],
            notes=[note],
        )

    assert result.status is CRMWriteStatus.SUCCEEDED
    assert client.calls and client.calls[0].notes[0].body.startswith("Customer asked")


@pytest.mark.asyncio
async def test_execute_uses_caller_supplied_idempotency_key(tenant: UUID, rep: UUID):
    client = _RecordingWriteClient()
    client.stage_success()
    writer = CRMWriter(write_client=client)

    with tenant_scope(tenant):
        await writer.execute(
            tenant_id=tenant,
            rep_id=rep,
            approver_rep_id=rep,
            platform=CRMPlatform.HUBSPOT,
            kind=CRMRecordKind.OPPORTUNITY,
            external_id="deal-write-1",
            field_updates=[_validated_stage_update()],
            idempotency_key="approval-123",
        )

    assert client.calls[0].idempotency_key == "approval-123"
