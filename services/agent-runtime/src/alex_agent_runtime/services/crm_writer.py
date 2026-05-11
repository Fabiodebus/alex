"""CRMWriter — dispatch approved CRM writes and audit the result.

Sits between the Approval Workflow and the Pipedream Integration
Layer. Once a rep has approved a batch of :class:`FieldUpdate` objects
(or :class:`CRMNote` objects), the Approval Workflow hands the
:class:`ValidatedFieldUpdate` list to :meth:`CRMWriter.execute` which:

1. Builds a :class:`CRMWriteRequest` with a stable ``idempotency_key``.
2. Dispatches via the injected :class:`CRMWriteClient` (Stub in dev /
   tests, Pipedream in prod).
3. Writes one ``audit_log`` row with field-level before/after values,
   ``action_type='crm.write'``, the dispatch outcome, and the
   ``idempotency_key`` in metadata.
4. On failure publishes ``crm.write_failed`` on the
   :class:`EventBus` so :class:`NotificationDelivery` (WO #13) can route
   a plain-language explanation to the rep. The writer itself never
   retries silently — that's the blueprint's safety rule.

Calls must happen inside an active ``tenant_scope(...)`` block — the
audit row's ``tenant_id`` comes from ``app.tenant_id``.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import structlog

from ..db import transactional_session
from ..schemas import (
    AuditLogEntry,
    CRMNote,
    CRMPlatform,
    CRMRecordKind,
    CRMWriteFailed,
    CRMWriteRequest,
    CRMWriteResult,
    CRMWriteStatus,
    ValidatedFieldUpdate,
)
from .audit_log import record_action
from .crm_write_client import CRMWriteClient, CRMWriteError
from .event_bus import EventBus

log = structlog.get_logger(__name__)


class CRMWriterError(RuntimeError):
    """Raised when the writer cannot even produce a CRMWriteRequest (e.g.
    empty batch). Dispatch failures are returned as
    :class:`CRMWriteResult` with status FAILED — they don't raise."""


class CRMWriter:
    def __init__(
        self,
        *,
        write_client: CRMWriteClient,
        event_bus: EventBus | None = None,
    ) -> None:
        self._client = write_client
        self._event_bus = event_bus

    async def execute(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        approver_rep_id: UUID | None,
        platform: CRMPlatform,
        kind: CRMRecordKind,
        external_id: str,
        field_updates: Sequence[ValidatedFieldUpdate] = (),
        notes: Sequence[CRMNote] = (),
        idempotency_key: str | None = None,
    ) -> CRMWriteResult:
        """Dispatch the write and emit audit + failure events."""
        if not field_updates and not notes:
            raise CRMWriterError(
                "CRMWriter.execute called with no field updates and no notes"
            )

        request = CRMWriteRequest(
            tenant_id=tenant_id,
            rep_id=rep_id,
            approver_rep_id=approver_rep_id,
            platform=platform,
            kind=kind,
            external_id=external_id,
            field_updates=list(field_updates),
            notes=list(notes),
            idempotency_key=idempotency_key or _build_idempotency_key(
                external_id=external_id, field_updates=field_updates, notes=notes
            ),
        )

        result: CRMWriteResult
        try:
            result = await self._client.write(request)
        except CRMWriteError as exc:
            log.warning(
                "crm_writer.dispatch_failed",
                platform=platform.value,
                external_id=external_id,
                status=exc.status,
            )
            result = CRMWriteResult(
                status=CRMWriteStatus.FAILED,
                platform=platform,
                external_id=external_id,
                succeeded_fields=[],
                failed_fields=[u.update.field_name for u in field_updates],
                raw_response={"transport_error": str(exc), "status": exc.status},
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.exception(
                "crm_writer.dispatch_unexpected_error",
                platform=platform.value,
                external_id=external_id,
            )
            result = CRMWriteResult(
                status=CRMWriteStatus.FAILED,
                platform=platform,
                external_id=external_id,
                succeeded_fields=[],
                failed_fields=[u.update.field_name for u in field_updates],
                raw_response={"unexpected_error": str(exc)},
            )

        audit_id = await self._write_audit_row(
            tenant_id=tenant_id,
            request=request,
            result=result,
        )
        result = result.model_copy(update={"audit_log_id": audit_id})

        if result.status is not CRMWriteStatus.SUCCEEDED and self._event_bus is not None:
            await self._event_bus.publish(
                "crm.write_failed",
                CRMWriteFailed(
                    tenant_id=tenant_id,
                    rep_id=rep_id,
                    platform=platform,
                    external_id=external_id,
                    field_names=result.failed_fields or [u.update.field_name for u in field_updates],
                    reason=_human_reason_from_result(result),
                    audit_log_id=audit_id,
                ),
            )

        log.info(
            "crm_writer.executed",
            platform=platform.value,
            external_id=external_id,
            status=result.status.value,
            succeeded=len(result.succeeded_fields),
            failed=len(result.failed_fields),
            audit_log_id=str(audit_id) if audit_id else None,
        )
        return result

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _write_audit_row(
        self,
        *,
        tenant_id: UUID,
        request: CRMWriteRequest,
        result: CRMWriteResult,
    ) -> UUID | None:
        try:
            async with transactional_session() as session:
                return await record_action(
                    session,
                    AuditLogEntry(
                        action_type="crm.write",
                        actor_rep_id=request.rep_id,
                        approver_rep_id=request.approver_rep_id,
                        target_type="crm_record",
                        # External CRM IDs are platform-specific strings,
                        # not UUIDs — keep target_id null and carry the
                        # platform/external_id in metadata.
                        target_id=None,
                        prompt={
                            "platform": request.platform.value,
                            "kind": request.kind.value,
                            "external_id": request.external_id,
                            "idempotency_key": request.idempotency_key,
                            "field_updates": [
                                {
                                    "field_name": u.update.field_name,
                                    "before": u.update.current_value,
                                    "after_proposed": u.update.proposed_value,
                                    "after_normalized": u.normalized_value,
                                    "platform_field_id": u.platform_field_id,
                                }
                                for u in request.field_updates
                            ],
                            "notes": [
                                {"title": n.title, "body": n.body}
                                for n in request.notes
                            ],
                        },
                        output={
                            "status": result.status.value,
                            "succeeded_fields": list(result.succeeded_fields),
                            "failed_fields": list(result.failed_fields),
                            "raw_response": _safe_json(result.raw_response),
                        },
                        metadata={
                            "tenant_id": str(tenant_id),
                            "platform": request.platform.value,
                            "external_id": request.external_id,
                        },
                    ),
                )
        except Exception:
            log.exception(
                "crm_writer.audit_write_failed",
                platform=request.platform.value,
                external_id=request.external_id,
            )
            return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _build_idempotency_key(
    *,
    external_id: str,
    field_updates: Sequence[ValidatedFieldUpdate],
    notes: Sequence[CRMNote],
) -> str:
    """Stable per-execute idempotency key.

    Combines the record id with a fresh uuid4 — feature workflows that
    want a deterministic key (e.g. tied to a specific approval id)
    should supply ``idempotency_key`` to :meth:`CRMWriter.execute`
    instead."""
    return f"{external_id}:{uuid4()}"


def _human_reason_from_result(result: CRMWriteResult) -> str:
    raw = result.raw_response or {}
    for key in ("transport_error", "unexpected_error", "error", "message", "detail"):
        if isinstance(raw.get(key), str):
            return raw[key]
    if result.failed_fields:
        return (
            f"CRM rejected {len(result.failed_fields)} field"
            f"{'s' if len(result.failed_fields) != 1 else ''}: "
            f"{', '.join(result.failed_fields)}"
        )
    return f"CRM write returned status '{result.status.value}'"


def _safe_json(value: Any) -> Any:
    """Best-effort coercion to JSON-friendly types — raw responses can
    be anything the Pipedream connector returned."""
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return {"unserializable": str(value)}


