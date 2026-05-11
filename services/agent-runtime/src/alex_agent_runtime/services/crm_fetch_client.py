"""HMAC-signed POST to the Pipedream `crm_fetch` workflow.

Used by ``CRMReader.fetch_record`` when the local MemoryStore cache
doesn't already hold the record. The wire contract is the same as the
other Pipedream outbound calls (X-Alex-Signature / X-Alex-Timestamp /
X-Tenant-Id).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from uuid import UUID

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import CRMFetchRequest, CRMPlatform, CRMRecord, CRMRecordKind
from .pipedream_client import _sign

log = structlog.get_logger(__name__)


class CRMFetchError(RuntimeError):
    def __init__(self, message: str, *, status: int, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class CRMFetchClient(Protocol):
    name: str

    async def fetch(self, request: CRMFetchRequest) -> dict[str, object] | None: ...


class StubCRMFetchClient:
    """Dev/test default: always misses, so MemoryStore is the cache."""

    name = "stub"

    async def fetch(self, request: CRMFetchRequest) -> dict[str, object] | None:
        log.warning("crm_fetch.stub_called", external_id=request.external_id)
        return None


class PipedreamCRMFetchClient:
    name = "pipedream"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient(timeout=15.0)
        self._owned_http = client is None

    async def close(self) -> None:
        if self._owned_http:
            await self._http.aclose()

    async def fetch(self, request: CRMFetchRequest) -> dict[str, object] | None:
        if not self._settings.alex_pipedream_crm_fetch_url:
            raise CRMFetchError(
                "ALEX_PIPEDREAM_CRM_FETCH_URL is unset; cannot do on-demand CRM fetch",
                status=0,
            )
        payload = request.model_dump(mode="json")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(request.tenant_id),
            "X-Alex-Timestamp": timestamp,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, timestamp, body.decode("utf-8")
            )
        response = await self._http.post(
            self._settings.alex_pipedream_crm_fetch_url,
            content=body,
            headers=headers,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            raise CRMFetchError(
                f"Pipedream crm_fetch returned {response.status_code}",
                status=response.status_code,
                body=parsed,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise CRMFetchError(
                f"Pipedream crm_fetch returned non-JSON: {exc}",
                status=response.status_code,
            ) from exc


def build_default_crm_fetch_client(settings: Settings | None = None) -> CRMFetchClient:
    s = settings or get_settings()
    if s.crm_fetch_provider == "pipedream":
        log.info("crm_fetch_client.selected", provider="pipedream")
        return PipedreamCRMFetchClient(s)
    log.warning("crm_fetch_client.selected", provider="stub")
    return StubCRMFetchClient()
