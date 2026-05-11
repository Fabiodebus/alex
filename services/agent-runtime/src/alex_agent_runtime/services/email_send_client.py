"""Dispatch an approved outbound email.

WO #18 (FollowUpDraftComposer) produces a ``PendingTask`` with
``task_type='email.send'``. After rep approval the
:class:`ApprovedActionDispatcher` routes the payload through one of
these clients.

Pattern mirrors :class:`CRMWriteClient` — Stub for dev/tests, Pipedream
for production.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import EmailSendRequest, EmailSendResult
from .pipedream_client import _sign

log = structlog.get_logger(__name__)


class EmailSendError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class EmailSendClient(Protocol):
    name: str

    async def send(self, request: EmailSendRequest) -> EmailSendResult: ...


class StubEmailSendClient:
    """Records calls and echoes success — used in dev + tests."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[EmailSendRequest] = []

    async def send(self, request: EmailSendRequest) -> EmailSendResult:
        self.calls.append(request)
        log.info(
            "email_send.stub.delivered",
            recipient_count=len(request.to),
            subject=request.subject,
            task_id=str(request.task_id) if request.task_id else None,
        )
        return EmailSendResult(
            delivered=True,
            provider="stub",
            provider_message_id=f"stub-{request.idempotency_key}",
            raw={"recipients": request.to, "subject": request.subject},
        )


class PipedreamEmailSendClient:
    name = "pipedream"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient(timeout=20.0)
        self._owned_http = client is None

    async def close(self) -> None:
        if self._owned_http:
            await self._http.aclose()

    async def send(self, request: EmailSendRequest) -> EmailSendResult:
        if not self._settings.alex_pipedream_email_send_url:
            raise EmailSendError(
                "ALEX_PIPEDREAM_EMAIL_SEND_URL is unset; cannot send email"
            )
        body = json.dumps(request.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(request.tenant_id),
            "X-Alex-Timestamp": ts,
            "Idempotency-Key": request.idempotency_key,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, ts, body.decode("utf-8")
            )
        response = await self._http.post(
            self._settings.alex_pipedream_email_send_url,
            content=body,
            headers=headers,
        )
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            raise EmailSendError(
                f"email_send returned {response.status_code}",
                status=response.status_code,
                body=parsed,
            )
        data = parsed if isinstance(parsed, dict) else {"raw": parsed}
        return EmailSendResult(
            delivered=bool(data.get("delivered", True)),
            provider=str(data.get("provider", "pipedream")),
            provider_message_id=data.get("provider_message_id"),
            raw=data,
        )


def build_default_email_send_client(settings: Settings | None = None) -> EmailSendClient:
    s = settings or get_settings()
    if s.email_send_provider == "pipedream":
        log.info("email_send_client.selected", provider="pipedream")
        return PipedreamEmailSendClient(s)
    log.warning("email_send_client.selected", provider="stub")
    return StubEmailSendClient()
