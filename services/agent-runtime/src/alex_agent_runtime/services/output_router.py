"""OutputRouter — the single dispatch path for all rep-facing outputs.

ADR-001 in the Notification Delivery blueprint is unambiguous: every
feature workflow hands a :class:`DeliveryRequest` (an "AgentOutput"
shaped payload) to this router, and nothing dispatches to a surface
directly. The router does three things:

1. Looks up the rep's :class:`DeliveryChannel` for the ``output_type``
   via :class:`DeliveryPreferenceRepo`. Default channel is Slack.
2. Records the attempt with :class:`DeliveryTracker` (which upserts on
   ``output_id`` so retries are idempotent).
3. Hands the :class:`DeliveryAttempt` to :class:`MessagingDeliveryClient`
   and updates the tracker with the surface response.

For the three EventBus topics that exist today, the router subscribes
on construction so the existing emit sites don't have to change:

* ``approval.requested`` → render an "Approval needed" output
* ``crm.write_failed``   → render a "CRM write failed" output
* ``approval.expired``   → render a "Pending task expired" output

Feature WOs may still call :meth:`deliver` directly for informational
outputs that aren't routed through the EventBus.

CRM-native delivery is in the schema but not implemented in v1 — the
router falls back to Slack with a metadata flag so the rep still
sees the output and the audit trail records that the preferred
channel was unavailable.
"""
from __future__ import annotations

from typing import Any

import structlog

from ..schemas import (
    ApprovalRequested,
    CRMWriteFailed,
    DeliveryAttempt,
    DeliveryChannel,
    DeliveryRequest,
    DeliveryStatus,
    DeliveryStatusValue,
    OutputType,
    TaskExpired,
)
from ..tenant_context import tenant_scope
from .approval_expiry_scan import TOPIC_APPROVAL_EXPIRED
from .approval_gate import TOPIC_APPROVAL_REQUESTED
from .crm_writer import TOPIC_CRM_WRITE_FAILED
from .delivery_preferences import DeliveryPreferenceRepo
from .delivery_tracker import DeliveryTracker
from .event_bus import EventBus
from .messaging_delivery_client import MessagingDeliveryClient, MessagingDeliveryError

log = structlog.get_logger(__name__)


class OutputRouter:
    def __init__(
        self,
        *,
        delivery_client: MessagingDeliveryClient,
        preferences: DeliveryPreferenceRepo,
        tracker: DeliveryTracker,
    ) -> None:
        self._client = delivery_client
        self._preferences = preferences
        self._tracker = tracker

    # ------------------------------------------------------------------
    # Explicit API
    # ------------------------------------------------------------------
    async def deliver(self, request: DeliveryRequest) -> DeliveryStatus:
        output_type = (
            request.output_type.value
            if isinstance(request.output_type, OutputType)
            else str(request.output_type)
        )
        channel = await self._preferences.get_channel(
            tenant_id=request.tenant_id,
            rep_id=request.rep_id,
            output_type=output_type,
        )
        fallback_metadata: dict[str, Any] = {}
        # CRM-native delivery isn't implemented in v1; fall back to
        # Slack and tag the row so the rep + audit trail see why.
        if channel is DeliveryChannel.CRM_NATIVE:
            log.warning(
                "output_router.crm_native_fallback",
                tenant_id=str(request.tenant_id),
                rep_id=str(request.rep_id),
                output_type=output_type,
            )
            fallback_metadata = {
                "preferred_channel": DeliveryChannel.CRM_NATIVE.value,
                "fallback_reason": "crm_native_not_implemented",
            }
            channel = DeliveryChannel.SLACK

        payload = {
            "title": request.title,
            "body": request.body,
            "metadata": {**request.metadata, **fallback_metadata},
            "task_id": str(request.task_id) if request.task_id else None,
            "output_type": output_type,
        }
        status = await self._tracker.record_pending(
            tenant_id=request.tenant_id,
            rep_id=request.rep_id,
            output_id=request.output_id,
            output_type=output_type,
            channel=channel,
            task_id=request.task_id,
            payload=payload,
        )
        attempt = DeliveryAttempt(
            tenant_id=request.tenant_id,
            rep_id=request.rep_id,
            output_id=request.output_id,
            output_type=output_type,
            task_id=request.task_id,
            title=request.title,
            body=request.body,
            metadata=payload["metadata"],
        )
        try:
            response = await self._client.deliver(channel=channel, attempt=attempt)
        except MessagingDeliveryError as exc:
            log.warning(
                "output_router.delivery_failed",
                output_id=request.output_id,
                channel=channel.value,
                status=exc.status,
            )
            failed = await self._tracker.mark_failed(
                tenant_id=request.tenant_id,
                output_id=request.output_id,
                response={"error": str(exc), "status": exc.status, "body": exc.body},
            )
            return failed or status
        except Exception as exc:  # pragma: no cover — defensive
            log.exception(
                "output_router.delivery_unexpected_error",
                output_id=request.output_id,
            )
            failed = await self._tracker.mark_failed(
                tenant_id=request.tenant_id,
                output_id=request.output_id,
                response={"unexpected_error": str(exc)},
            )
            return failed or status

        delivered = await self._tracker.mark_delivered(
            tenant_id=request.tenant_id,
            output_id=request.output_id,
            response=response if isinstance(response, dict) else {"raw": response},
        )
        log.info(
            "output_router.delivered",
            output_id=request.output_id,
            channel=channel.value,
            status=DeliveryStatusValue.DELIVERED.value,
        )
        return delivered or status

    # ------------------------------------------------------------------
    # EventBus adapters
    # ------------------------------------------------------------------
    async def _on_approval_requested(self, payload: ApprovalRequested) -> None:
        title = payload.title or f"Approval needed: {payload.task_type}"
        body = (
            f"A new {payload.task_type} is awaiting your approval. "
            f"Expires {payload.deadline.isoformat()}."
        )
        with tenant_scope(payload.tenant_id):
            await self.deliver(
                DeliveryRequest(
                    tenant_id=payload.tenant_id,
                    rep_id=payload.rep_id,
                    output_id=f"approval:{payload.task_id}",
                    output_type=OutputType.APPROVAL_REQUEST,
                    task_id=payload.task_id,
                    title=title,
                    body=body,
                    metadata={
                        "deadline": payload.deadline.isoformat(),
                        "task_type": payload.task_type,
                        "payload": payload.payload,
                    },
                )
            )

    async def _on_crm_write_failed(self, payload: CRMWriteFailed) -> None:
        title = f"CRM write failed on {payload.platform.value}"
        fields = ", ".join(payload.field_names) if payload.field_names else "unknown"
        body = (
            f"The proposed write to {payload.platform.value}/{payload.external_id} "
            f"didn't go through. Affected fields: {fields}. Reason: {payload.reason}."
        )
        with tenant_scope(payload.tenant_id):
            await self.deliver(
                DeliveryRequest(
                    tenant_id=payload.tenant_id,
                    rep_id=payload.rep_id,
                    output_id=f"crm_write_failed:{payload.platform.value}:{payload.external_id}:"
                    f"{payload.audit_log_id}",
                    output_type=OutputType.CRM_WRITE_FAILED,
                    title=title,
                    body=body,
                    metadata={
                        "platform": payload.platform.value,
                        "external_id": payload.external_id,
                        "field_names": payload.field_names,
                        "audit_log_id": (
                            str(payload.audit_log_id) if payload.audit_log_id else None
                        ),
                    },
                )
            )

    async def _on_approval_expired(self, payload: TaskExpired) -> None:
        title = f"Pending task expired ({payload.task_type})"
        body = (
            f"Your {payload.task_type} approval task expired at "
            f"{payload.deadline.isoformat()} without action."
        )
        with tenant_scope(payload.tenant_id):
            await self.deliver(
                DeliveryRequest(
                    tenant_id=payload.tenant_id,
                    rep_id=payload.rep_id,
                    output_id=f"approval_expired:{payload.task_id}",
                    output_type=OutputType.NOTIFICATION,
                    task_id=payload.task_id,
                    title=title,
                    body=body,
                    metadata={
                        "task_type": payload.task_type,
                        "deadline": payload.deadline.isoformat(),
                    },
                )
            )


def attach_router(*, bus: EventBus, router: OutputRouter) -> None:
    """Subscribe the router to the three v1 EventBus topics."""
    bus.subscribe(TOPIC_APPROVAL_REQUESTED, router._on_approval_requested)
    bus.subscribe(TOPIC_CRM_WRITE_FAILED, router._on_crm_write_failed)
    bus.subscribe(TOPIC_APPROVAL_EXPIRED, router._on_approval_expired)
