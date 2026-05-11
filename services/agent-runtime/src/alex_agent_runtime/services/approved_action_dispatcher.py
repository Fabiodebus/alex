"""ApprovedActionDispatcher — routes ``approval.approved`` to executors.

The blueprint's ADR-001 makes the runtime approval-first: every
external action sits behind a :class:`PendingTask` and only fires
after the rep approves. The :class:`ApprovalHandler` writes the audit
row and publishes ``approval.approved``; this dispatcher subscribes
and maps the task's ``task_type`` to a concrete executor:

* ``crm.write`` → :class:`CRMWriter` (built in WO #10)
* future ``email.send`` / ``doc.upload`` add their own subscribers
  inside their feature WOs.

Unknown task types are logged at WARNING and dropped — the system
contract guarantees an approved task was already audit-logged, so a
missing executor is loud but recoverable (a follow-on WO can ship
the executor).
"""
from __future__ import annotations

import structlog

from uuid import uuid4

from ..schemas import (
    CRMNote,
    CRMPlatform,
    CRMRecordKind,
    EmailSendRequest,
    FieldUpdate,
    TaskApproved,
    ValidatedFieldUpdate,
)
from ..tenant_context import tenant_scope
from .crm_validator import CRMValidator
from .crm_writer import CRMWriter
from .email_send_client import EmailSendClient, EmailSendError

log = structlog.get_logger(__name__)


class ApprovedActionDispatcher:
    """Subscribes to ``approval.approved`` and routes by ``task_type``."""

    def __init__(
        self,
        *,
        crm_writer: CRMWriter,
        crm_validator: CRMValidator,
        email_send_client: EmailSendClient | None = None,
    ) -> None:
        self._crm_writer = crm_writer
        self._crm_validator = crm_validator
        self._email_send_client = email_send_client

    async def handle_approved(self, event: TaskApproved) -> None:
        handler = {
            "crm.write": self._dispatch_crm_write,
            "email.send": self._dispatch_email_send,
        }.get(event.task_type)
        if handler is None:
            log.warning(
                "approved_action_dispatcher.unsupported_task_type",
                task_type=event.task_type,
                task_id=str(event.task_id),
            )
            return
        await handler(event)

    # ------------------------------------------------------------------
    # crm.write executor
    # ------------------------------------------------------------------
    async def _dispatch_crm_write(self, event: TaskApproved) -> None:
        """Execute an approved CRM write via :class:`CRMWriter`.

        Payload shape (set by the feature workflow that opened the
        PendingTask, e.g. CRM Notes & Updates in a later WO):

        ``{
            "platform": "hubspot",
            "kind": "opportunity",
            "external_id": "deal-123",
            "field_updates": [
                {"field_name": "...", "current_value": ..., "proposed_value": ...},
                ...
            ],
            "notes": [{"body": "...", "title": null}, ...]
        }``

        The dispatcher re-validates each update through :class:`CRMValidator`
        before calling :meth:`CRMWriter.execute` so an editing rep can't
        sneak through a bad value. Rejected updates are logged and the
        write is dropped (CRMWriter will not see them).
        """
        payload = event.payload or {}
        try:
            platform = CRMPlatform(payload["platform"])
            kind = CRMRecordKind(payload["kind"])
            external_id = str(payload["external_id"])
        except (KeyError, ValueError) as exc:
            log.warning(
                "approved_action_dispatcher.invalid_crm_write_payload",
                task_id=str(event.task_id),
                error=str(exc),
            )
            return

        validated: list[ValidatedFieldUpdate] = []
        for raw in payload.get("field_updates") or []:
            try:
                update = FieldUpdate(
                    platform=platform,
                    kind=kind,
                    external_id=external_id,
                    field_name=raw["field_name"],
                    current_value=raw.get("current_value"),
                    proposed_value=raw.get("proposed_value"),
                )
            except KeyError:
                log.warning(
                    "approved_action_dispatcher.malformed_field_update",
                    task_id=str(event.task_id),
                    raw=raw,
                )
                continue
            result = self._crm_validator.validate(update)
            if result.is_valid and result.validated is not None:
                validated.append(result.validated)
            else:
                log.warning(
                    "approved_action_dispatcher.field_update_rejected_post_approval",
                    task_id=str(event.task_id),
                    field_name=update.field_name,
                    code=result.error.code if result.error else None,
                )

        notes: list[CRMNote] = []
        for raw_note in payload.get("notes") or []:
            try:
                notes.append(
                    CRMNote(
                        platform=platform,
                        kind=kind,
                        external_id=external_id,
                        body=raw_note["body"],
                        title=raw_note.get("title"),
                    )
                )
            except KeyError:
                log.warning(
                    "approved_action_dispatcher.malformed_note",
                    task_id=str(event.task_id),
                    raw=raw_note,
                )

        if not validated and not notes:
            log.info(
                "approved_action_dispatcher.crm_write_empty_after_revalidate",
                task_id=str(event.task_id),
            )
            return

        with tenant_scope(event.tenant_id):
            result = await self._crm_writer.execute(
                tenant_id=event.tenant_id,
                rep_id=event.rep_id,
                approver_rep_id=event.rep_id,
                platform=platform,
                kind=kind,
                external_id=external_id,
                field_updates=validated,
                notes=notes,
                idempotency_key=f"task:{event.task_id}",
            )
        log.info(
            "approved_action_dispatcher.crm_write_dispatched",
            task_id=str(event.task_id),
            status=result.status.value,
            succeeded=len(result.succeeded_fields),
            failed=len(result.failed_fields),
            audit_log_id=str(result.audit_log_id) if result.audit_log_id else None,
        )


    # ------------------------------------------------------------------
    # email.send executor (WO #18)
    # ------------------------------------------------------------------
    async def _dispatch_email_send(self, event: TaskApproved) -> None:
        """Send an approved follow-up email via :class:`EmailSendClient`.

        Payload shape produced by FollowUpDraftComposer:
            {subject, body, to, cc?, in_reply_to?, idempotency_key,
             language, de_register, calendar_event_id, used_transcript}

        On edit, the rep's revised body/subject lands in the payload via
        ``edited_output`` — ApprovalHandler already merges that into
        ``payload`` for us. Our job is just to validate + dispatch."""
        if self._email_send_client is None:
            log.warning(
                "approved_action_dispatcher.email_send_unavailable",
                task_id=str(event.task_id),
            )
            return
        payload = event.payload or {}
        to = payload.get("to") or []
        subject = payload.get("subject") or ""
        body = payload.get("body") or ""
        if not isinstance(to, list) or not to or not subject or not body:
            log.warning(
                "approved_action_dispatcher.malformed_email_payload",
                task_id=str(event.task_id),
                missing=[k for k in ("to", "subject", "body") if not payload.get(k)],
            )
            return
        idempotency_key = (
            payload.get("idempotency_key")
            or f"task:{event.task_id}:{uuid4()}"
        )
        request = EmailSendRequest(
            tenant_id=event.tenant_id,
            rep_id=event.rep_id,
            task_id=event.task_id,
            to=[str(x) for x in to],
            cc=[str(x) for x in payload.get("cc") or []],
            bcc=[str(x) for x in payload.get("bcc") or []],
            subject=str(subject),
            body=str(body),
            in_reply_to=payload.get("in_reply_to"),
            idempotency_key=str(idempotency_key),
        )
        try:
            with tenant_scope(event.tenant_id):
                result = await self._email_send_client.send(request)
        except EmailSendError as exc:
            log.warning(
                "approved_action_dispatcher.email_send_failed",
                task_id=str(event.task_id),
                status=exc.status,
            )
            return
        log.info(
            "approved_action_dispatcher.email_send_dispatched",
            task_id=str(event.task_id),
            delivered=result.delivered,
            provider=result.provider,
        )


def attach_dispatcher(*, bus, dispatcher: ApprovedActionDispatcher) -> None:
    """Subscribe the dispatcher to ``approval.approved``."""
    from .approval_handler import TOPIC_APPROVAL_APPROVED

    bus.subscribe(TOPIC_APPROVAL_APPROVED, dispatcher.handle_approved)
