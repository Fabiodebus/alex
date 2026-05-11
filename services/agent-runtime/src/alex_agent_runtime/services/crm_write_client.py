"""HMAC-signed POST to the Pipedream `crm_write` workflow.

Used by :class:`alex_agent_runtime.services.crm_writer.CRMWriter` to
dispatch validated, rep-approved write requests. The wire contract is
the same as the other Pipedream outbound calls
(``X-Alex-Signature`` / ``X-Alex-Timestamp`` / ``X-Tenant-Id``).

The stub variant exists for tests and the dev provider mode: it
returns a synthetic ``CRMWriteResult`` so the runtime can be exercised
end-to-end without a live Pipedream workflow.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import CRMWriteRequest, CRMWriteResult, CRMWriteStatus
from .pipedream_client import _sign

log = structlog.get_logger(__name__)


class CRMWriteError(RuntimeError):
    def __init__(self, message: str, *, status: int, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class CRMWriteClient(Protocol):
    name: str

    async def write(self, request: CRMWriteRequest) -> CRMWriteResult: ...


class StubCRMWriteClient:
    """Dev/test default: always succeeds and echoes the request shape.

    Tests that want to simulate failure should use a custom client (see
    ``tests/test_crm_writer.py`` for a small recording stub)."""

    name = "stub"

    async def write(self, request: CRMWriteRequest) -> CRMWriteResult:
        succeeded = [u.update.field_name for u in request.field_updates]
        log.info(
            "crm_write.stub.echo",
            platform=request.platform.value,
            external_id=request.external_id,
            fields=succeeded,
            notes=len(request.notes),
        )
        return CRMWriteResult(
            status=CRMWriteStatus.SUCCEEDED,
            platform=request.platform,
            external_id=request.external_id,
            succeeded_fields=succeeded,
            raw_response={"backend": "stub"},
        )


class PipedreamCRMWriteClient:
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

    async def write(self, request: CRMWriteRequest) -> CRMWriteResult:
        if not self._settings.alex_pipedream_crm_write_url:
            raise CRMWriteError(
                "ALEX_PIPEDREAM_CRM_WRITE_URL is unset; cannot dispatch CRM writes",
                status=0,
            )
        payload = request.model_dump(mode="json")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(request.tenant_id),
            "X-Alex-Timestamp": timestamp,
            "Idempotency-Key": request.idempotency_key,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, timestamp, body.decode("utf-8")
            )
        response = await self._http.post(
            self._settings.alex_pipedream_crm_write_url,
            content=body,
            headers=headers,
        )
        if response.status_code >= 400:
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            raise CRMWriteError(
                f"Pipedream crm_write returned {response.status_code}",
                status=response.status_code,
                body=parsed,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise CRMWriteError(
                f"Pipedream crm_write returned non-JSON: {exc}",
                status=response.status_code,
            ) from exc

        # The Pipedream workflow is expected to return a CRMWriteResult-
        # shaped JSON. Be defensive about the precise envelope so swaps
        # to a thinner connector response don't break the runtime.
        return _result_from_pipedream(request, data)


def _result_from_pipedream(request: CRMWriteRequest, data: dict[str, object]) -> CRMWriteResult:
    succeeded = data.get("succeeded_fields") or [u.update.field_name for u in request.field_updates]
    failed = data.get("failed_fields") or []
    status_raw = data.get("status") or ("succeeded" if not failed else "failed")
    try:
        status = CRMWriteStatus(status_raw)
    except ValueError:
        status = CRMWriteStatus.FAILED
    return CRMWriteResult(
        status=status,
        platform=request.platform,
        external_id=request.external_id,
        succeeded_fields=list(succeeded),  # type: ignore[arg-type]
        failed_fields=list(failed),  # type: ignore[arg-type]
        raw_response=data if isinstance(data, dict) else {"raw": data},
    )


def build_default_crm_write_client(settings: Settings | None = None) -> CRMWriteClient:
    s = settings or get_settings()
    if s.crm_write_provider == "pipedream":
        log.info("crm_write_client.selected", provider="pipedream")
        return PipedreamCRMWriteClient(s)
    log.warning("crm_write_client.selected", provider="stub")
    return StubCRMWriteClient()
