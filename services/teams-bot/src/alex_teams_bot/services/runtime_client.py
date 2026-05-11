"""HMAC-signed POST helpers to the Agent Runtime — mirror of slack-bot's client."""
from __future__ import annotations

import json

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import ApprovalCallback, FeedbackEvent
from .signing import sign_outbound

log = structlog.get_logger(__name__)


class RuntimeClientError(RuntimeError):
    def __init__(self, message: str, *, status: int, body: object | None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RuntimeClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http = client or httpx.AsyncClient(timeout=10.0)
        self._owned_http = client is None

    async def close(self) -> None:
        if self._owned_http:
            await self._http.aclose()

    async def post_approval_callback(self, *, tenant_id: str, callback: ApprovalCallback) -> dict[str, object]:
        return await self._post(
            path="/callbacks",
            tenant_id=tenant_id,
            payload=callback.model_dump(mode="json"),
        )

    async def post_feedback(self, *, tenant_id: str, event: FeedbackEvent) -> dict[str, object]:
        return await self._post(
            path="/callbacks",
            tenant_id=tenant_id,
            payload={
                "task_id": str(event.task_id),
                "rep_id": str(event.rep_id),
                "action": "feedback",
                "feedback": event.note,
                "edited_output": {"rating": event.rating},
            },
        )

    async def _post(self, *, path: str, tenant_id: str, payload: dict[str, object]) -> dict[str, object]:
        if not self._settings.alex_agent_runtime_url:
            raise RuntimeClientError("ALEX_AGENT_RUNTIME_URL is unset", status=0, body=None)
        body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        url = f"{self._settings.alex_agent_runtime_url.rstrip('/')}{path}"
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": tenant_id,
        }
        if self._settings.alex_webhook_secret:
            sig, ts = sign_outbound(secret=self._settings.alex_webhook_secret, body=body)
            headers["X-Alex-Signature"] = sig
            headers["X-Alex-Timestamp"] = ts
        response = await self._http.post(url, content=body, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            raise RuntimeClientError(
                f"Agent Runtime rejected callback ({response.status_code})",
                status=response.status_code,
                body=parsed,
            )
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
