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
from ..schemas import CRMFetchRequest, CRMPlatform, CRMRecord, CRMRecordKind, OnboardingConnector
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


class PipedreamConnectCRMFetchClient:
    """Fetch CRM records via Pipedream Connect proxy (WO #24).

    Looks up the rep's Pipedream account_id for the requested platform,
    constructs the upstream provider's REST URL (Close, HubSpot, …),
    and asks Pipedream's Connect proxy to inject the rep's OAuth
    credentials and forward.

    Only Close is wired in v1 — the other CRM platforms fall back to
    raising ``CRMFetchError`` until per-platform URL mapping lands.
    """

    name = "pipedream_connect"

    # Close API base. Each ``kind`` maps to one endpoint segment.
    _CLOSE_PATHS: dict[CRMRecordKind, str] = {
        CRMRecordKind.OPPORTUNITY: "opportunity",
        CRMRecordKind.CONTACT: "contact",
        # Close models accounts as "leads".
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
            raise CRMFetchError(
                "PipedreamConnectCRMFetchClient requires Connect credentials in Settings",
                status=0,
            )
        self._client = connect_client
        self._owned_client = owned
        self._resolver = resolver or DatabaseConnectAccountResolver()

    async def close(self) -> None:
        if self._owned_client:
            await self._client.close()

    async def fetch(self, request: CRMFetchRequest) -> dict[str, object] | None:
        connector = _connector_for_platform(request.platform)
        if connector is None:
            raise CRMFetchError(
                f"Pipedream Connect fetch not yet wired for platform "
                f"{request.platform.value}",
                status=0,
            )
        account_id = await self._resolver.resolve(
            tenant_id=request.tenant_id, connector=connector
        )
        if account_id is None:
            log.warning(
                "crm_fetch.pipedream_connect.no_account",
                tenant_id=str(request.tenant_id),
                platform=request.platform.value,
            )
            return None
        url = self._url_for(platform=request.platform, kind=request.kind, external_id=request.external_id)
        try:
            envelope = await self._client.proxy_request(
                external_user_id=f"tenant:{request.tenant_id}",  # any non-empty value; Pipedream ties the call to account_id
                account_id=account_id,
                url=url,
                method="GET",
            )
        except PipedreamConnectError as exc:
            if exc.status == 404:
                return None
            raise CRMFetchError(
                f"Pipedream Connect proxy returned {exc.status}",
                status=exc.status,
                body=exc.body,
            ) from exc
        # Pipedream wraps the upstream response in ``{statusCode, body, headers}``.
        status_code = envelope.get("statusCode") if isinstance(envelope, dict) else None
        if status_code == 404:
            return None
        if isinstance(status_code, int) and status_code >= 400:
            raise CRMFetchError(
                f"Upstream {request.platform.value} returned {status_code} via Connect proxy",
                status=status_code,
                body=envelope,
            )
        body = envelope.get("body") if isinstance(envelope, dict) else None
        if isinstance(body, str):
            import json as _json

            try:
                body = _json.loads(body)
            except ValueError:
                return None
        return body if isinstance(body, dict) else None

    def _url_for(
        self,
        *,
        platform: CRMPlatform,
        kind: CRMRecordKind,
        external_id: str,
    ) -> str:
        if platform is CRMPlatform.CLOSE:
            path = self._CLOSE_PATHS.get(kind)
            if path is None:
                raise CRMFetchError(
                    f"Close adapter has no URL mapping for kind {kind.value}",
                    status=0,
                )
            return f"https://api.close.com/api/v1/{path}/{external_id}/"
        # Other platforms wired later (WO #25+).
        raise CRMFetchError(
            f"Pipedream Connect URL builder not implemented for {platform.value}",
            status=0,
        )


def _connector_for_platform(platform: CRMPlatform) -> OnboardingConnector | None:
    return {
        CRMPlatform.CLOSE: OnboardingConnector.CLOSE,
    }.get(platform)


def build_default_crm_fetch_client(settings: Settings | None = None) -> CRMFetchClient:
    s = settings or get_settings()
    if s.crm_fetch_provider == "pipedream_connect":
        log.info("crm_fetch_client.selected", provider="pipedream_connect")
        return PipedreamConnectCRMFetchClient(s)
    if s.crm_fetch_provider == "pipedream":
        log.info("crm_fetch_client.selected", provider="pipedream")
        return PipedreamCRMFetchClient(s)
    log.warning("crm_fetch_client.selected", provider="stub")
    return StubCRMFetchClient()
