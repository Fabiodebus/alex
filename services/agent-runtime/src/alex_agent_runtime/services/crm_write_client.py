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
from ..schemas import (
    CRMPlatform,
    CRMRecordKind,
    CRMWriteRequest,
    CRMWriteResult,
    CRMWriteStatus,
    OnboardingConnector,
)
from .connect_account_resolver import (
    ConnectAccountResolver,
    DatabaseConnectAccountResolver,
)
from .pipedream_client import _sign
from .pipedream_connect_client import (
    PipedreamConnectClient,
    PipedreamConnectError,
    build_pipedream_connect_client,
)

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


class PipedreamConnectCRMWriteClient:
    """Write CRM updates via Pipedream Connect proxy (WO #24).

    Iterates field_updates into per-record PUTs to the upstream CRM,
    then attaches each note via the CRM's note endpoint. All calls go
    through Pipedream's Connect proxy keyed on the rep's account_id.

    v1 wires Close only. Other CRM platforms raise until per-platform
    URL builders land in a follow-up WO.
    """

    name = "pipedream_connect"

    _CLOSE_PATHS: dict[CRMRecordKind, str] = {
        CRMRecordKind.OPPORTUNITY: "opportunity",
        CRMRecordKind.CONTACT: "contact",
        CRMRecordKind.ACCOUNT: "lead",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        connect_client: PipedreamConnectClient | None = None,
        resolver: ConnectAccountResolver | None = None,
    ) -> None:
        self._settings = settings
        owned = connect_client is None
        if connect_client is None:
            connect_client = build_pipedream_connect_client(settings)
        if connect_client is None:
            raise CRMWriteError(
                "PipedreamConnectCRMWriteClient requires Connect credentials in Settings",
                status=0,
            )
        self._client = connect_client
        self._owned_client = owned
        self._resolver = resolver or DatabaseConnectAccountResolver()

    async def close(self) -> None:
        if self._owned_client:
            await self._client.close()

    async def write(self, request: CRMWriteRequest) -> CRMWriteResult:
        connector = _connector_for_platform(request.platform)
        if connector is None:
            raise CRMWriteError(
                f"Pipedream Connect write not yet wired for platform "
                f"{request.platform.value}",
                status=0,
            )
        account_id = await self._resolver.resolve(
            tenant_id=request.tenant_id,
            connector=connector,
            rep_id=request.rep_id,
        )
        if account_id is None:
            raise CRMWriteError(
                f"No Pipedream Connect account for tenant/rep on {connector.value}",
                status=0,
            )

        succeeded: list[str] = []
        failed: list[str] = []
        last_response: object = None

        # 1. Field updates → one PUT to the record's primary endpoint.
        if request.field_updates:
            record_url = self._record_url(
                platform=request.platform,
                kind=request.kind,
                external_id=request.external_id,
            )
            body = {
                u.update.field_name: u.update.proposed_value
                for u in request.field_updates
            }
            envelope = await self._proxy(
                request=request,
                account_id=account_id,
                url=record_url,
                method="PUT",
                json_body=body,
            )
            last_response = envelope
            status_code = (envelope or {}).get("statusCode")
            if isinstance(status_code, int) and status_code < 400:
                succeeded.extend(body.keys())
            else:
                failed.extend(body.keys())

        # 2. Notes → POST to Close's `/api/v1/activity/note/` endpoint
        #    (Close models notes as activities). Each note becomes one
        #    activity row scoped to the opportunity / lead.
        if request.notes and request.platform is CRMPlatform.CLOSE:
            note_url = "https://api.close.com/api/v1/activity/note/"
            for note in request.notes:
                envelope = await self._proxy(
                    request=request,
                    account_id=account_id,
                    url=note_url,
                    method="POST",
                    json_body={
                        # Close calls the parent record's id `lead_id` for
                        # account notes, `opportunity_id` for opportunity notes.
                        ("opportunity_id"
                         if request.kind is CRMRecordKind.OPPORTUNITY
                         else "lead_id"): request.external_id,
                        "note": note.body,
                    },
                )
                last_response = envelope
                status_code = (envelope or {}).get("statusCode")
                if not (isinstance(status_code, int) and status_code < 400):
                    failed.append(f"note:{note.body[:20]}")

        # CRMWriteStatus has no PARTIAL value, so any failure surfaces as
        # FAILED even when some fields landed — the ``succeeded_fields``
        # list still records what did make it through.
        crm_status = CRMWriteStatus.FAILED if failed else CRMWriteStatus.SUCCEEDED

        return CRMWriteResult(
            status=crm_status,
            platform=request.platform,
            external_id=request.external_id,
            succeeded_fields=succeeded,
            failed_fields=failed,
            raw_response=last_response if isinstance(last_response, dict) else {"raw": last_response},
        )

    async def _proxy(
        self,
        *,
        request: CRMWriteRequest,
        account_id: str,
        url: str,
        method: str,
        json_body: object,
    ) -> dict[str, object]:
        try:
            return await self._client.proxy_request(
                external_user_id=f"rep:{request.rep_id}",
                account_id=account_id,
                url=url,
                method=method,
                json_body=json_body,
            )
        except PipedreamConnectError as exc:
            raise CRMWriteError(
                f"Pipedream Connect proxy returned {exc.status}",
                status=exc.status,
                body=exc.body,
            ) from exc

    def _record_url(
        self, *, platform: CRMPlatform, kind: CRMRecordKind, external_id: str
    ) -> str:
        if platform is CRMPlatform.CLOSE:
            path = self._CLOSE_PATHS.get(kind)
            if path is None:
                raise CRMWriteError(
                    f"Close adapter has no URL mapping for kind {kind.value}",
                    status=0,
                )
            return f"https://api.close.com/api/v1/{path}/{external_id}/"
        raise CRMWriteError(
            f"Pipedream Connect URL builder not implemented for {platform.value}",
            status=0,
        )


def _connector_for_platform(platform: CRMPlatform) -> OnboardingConnector | None:
    return {
        CRMPlatform.CLOSE: OnboardingConnector.CLOSE,
    }.get(platform)


def build_default_crm_write_client(settings: Settings | None = None) -> CRMWriteClient:
    s = settings or get_settings()
    if s.crm_write_provider == "pipedream_connect":
        log.info("crm_write_client.selected", provider="pipedream_connect")
        return PipedreamConnectCRMWriteClient(s)
    if s.crm_write_provider == "pipedream":
        log.info("crm_write_client.selected", provider="pipedream")
        return PipedreamCRMWriteClient(s)
    log.warning("crm_write_client.selected", provider="stub")
    return StubCRMWriteClient()
