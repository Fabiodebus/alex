"""HMAC-signed POST to the Pipedream oauth_relay workflow."""
from __future__ import annotations

import json

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import OAuthToken
from .signing import sign_outbound

log = structlog.get_logger(__name__)


class PipedreamOAuthError(RuntimeError):
    def __init__(self, message: str, *, status: int, body: object | None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PipedreamOAuthClient:
    """Forwards completed OAuth tokens to the Pipedream relay."""

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

    async def relay(self, token: OAuthToken) -> dict[str, object]:
        if not self._settings.alex_pipedream_oauth_relay_url:
            raise PipedreamOAuthError(
                "ALEX_PIPEDREAM_OAUTH_RELAY_URL is unset", status=0, body=None
            )
        body = json.dumps(token.model_dump(mode="json"), default=str, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(token.tenant_id),
        }
        if self._settings.alex_webhook_secret:
            signature, timestamp = sign_outbound(secret=self._settings.alex_webhook_secret, body=body)
            headers["X-Alex-Signature"] = signature
            headers["X-Alex-Timestamp"] = timestamp
        url = self._settings.alex_pipedream_oauth_relay_url
        log.info("pipedream_oauth_client.relay", url=url, signed=bool("X-Alex-Signature" in headers))
        response = await self._http.post(url, content=body, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            raise PipedreamOAuthError(
                f"Pipedream rejected OAuth relay ({response.status_code})",
                status=response.status_code,
                body=parsed,
            )
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
