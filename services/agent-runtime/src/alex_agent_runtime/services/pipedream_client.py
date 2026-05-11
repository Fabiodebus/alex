"""Outbound HTTP client for Pipedream-hosted execution workflows.

Mirrors the wire contract verified end-to-end in
services/pipedream/tests/verifier.test.mjs — HMAC-SHA256 over
``f"{timestamp}.{rawBody}"`` with ``X-Alex-Signature`` and
``X-Alex-Timestamp`` headers, plus ``X-Tenant-Id`` for routing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
import hmac

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import (
    ActionRequest,
    ConnectionStatusView,
    DryRunRequest,
    DryRunResponse,
)

log = structlog.get_logger(__name__)

SIGNATURE_PREFIX = "sha256="


class PipedreamConfigError(RuntimeError):
    pass


class PipedreamExecutionError(RuntimeError):
    def __init__(self, message: str, *, status: int, body: object | None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _sign(secret: str, timestamp: str, body: str) -> str:
    payload = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


class PipedreamClient:
    """Thin client mapping ActionRequest/DryRunRequest/ConnectionStatus
    query to the per-source Pipedream workflow URLs."""

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

    # ------------------------------------------------------------------
    # ActionRequest
    # ------------------------------------------------------------------
    async def dispatch(self, request: ActionRequest) -> dict[str, object]:
        url = self._resolve_action_url(request)
        return await self._post(url, request.model_dump(mode="json"))

    async def dry_run(self, request: DryRunRequest) -> DryRunResponse:
        url = self._workflow_url("dry_run_crm_write")
        body = await self._post(url, request.model_dump(mode="json"))
        return DryRunResponse.model_validate(body)

    async def fetch_connection_status(
        self, *, tenant_id, rep_id, source: str
    ) -> ConnectionStatusView | None:
        """Read-through to the Agent Runtime's own oauth_connections store.

        The Pipedream side persists the vault entry; the runtime's
        connection_repo is the source of truth for status. This method
        exists so feature WOs can call ``client.fetch_connection_status``
        without coupling to the repo directly (testability).
        """
        from .connection_repo import get_connection  # local import to avoid cycle

        return await get_connection(tenant_id=tenant_id, rep_id=rep_id, source=source)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _post(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload, default=str, separators=(",", ":"))
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(payload.get("tenant_id", "")),
            "X-Alex-Timestamp": timestamp,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, timestamp, body
            )
        elif self._settings.webhook_signing_enforced:
            # Defensive: webhook_signing_enforced is true iff the secret is
            # non-empty, but covering the contradictory state explicitly
            # makes a misconfiguration loud rather than silent.
            raise PipedreamConfigError(
                "webhook signing enforced but secret missing"
            )
        log.info(
            "pipedream_client.post",
            url=url,
            tenant_id=headers.get("X-Tenant-Id"),
            signed=bool("X-Alex-Signature" in headers),
        )
        response = await self._http.post(url, content=body, headers=headers)
        parsed: object | None
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            raise PipedreamExecutionError(
                f"Pipedream workflow rejected request ({response.status_code})",
                status=response.status_code,
                body=parsed,
            )
        if isinstance(parsed, dict):
            return parsed
        return {"raw": parsed}

    def _resolve_action_url(self, request: ActionRequest) -> str:
        """Map (action_type, target_system) → workflow slug.

        Feature WOs may extend this; for now the mapping is exhaustive
        over the reference connectors and unknown combos raise loudly so
        regressions don't fall through to a silent 404.
        """
        key = (request.action_type.value, request.target_system)
        mapping = {
            ("crm.write", "hubspot"): "hubspot_crm_write",
            ("email.send", "gmail"): "gmail_send_message",
            ("doc.upload", "google_drive"): "google_drive_upload",
        }
        slug = mapping.get(key)
        if slug is None:
            raise PipedreamConfigError(
                f"no Pipedream workflow registered for {request.action_type.value} "
                f"→ {request.target_system}"
            )
        return self._workflow_url(slug)

    def _workflow_url(self, slug: str) -> str:
        base = self._settings.pipedream_base_url
        if not base:
            raise PipedreamConfigError(
                "PIPEDREAM_BASE_URL is unset; configure it before dispatching actions"
            )
        return f"{base.rstrip('/')}/{slug}"
