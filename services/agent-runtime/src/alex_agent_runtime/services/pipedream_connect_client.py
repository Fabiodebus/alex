"""Async client for Pipedream Connect's REST API (WO #24).

Pipedream Connect lets us delegate OAuth entirely to Pipedream. From the
runtime's perspective the lifecycle is:

1. ``create_connect_token`` — server-side call to mint a short-lived token
   scoped to one of our reps (``external_user_id``). The response includes
   a ``connect_link_url`` we hand to the rep via Slack. The rep clicks it,
   authorises the app inside Pipedream's UI, and Pipedream stores the
   resulting OAuth credentials on its side.

2. A webhook fires from Pipedream when the rep finishes connecting. The
   payload carries the ``account.id`` we use as our ``token_ref`` going
   forward.

3. To call provider APIs (Gmail, Calendar, Close, ...) we use
   ``proxy_request`` — Pipedream injects the rep's credentials and forwards
   to the upstream URL. We never see raw OAuth tokens.

Authentication to Pipedream itself is OAuth 2.0 client_credentials. We
exchange the runtime's client_id + client_secret for a short-lived bearer
token (TTL = 3600 s) and cache it for the lifetime of the process. On 401
we refresh once and retry.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from ..config import Settings

log = structlog.get_logger(__name__)


class PipedreamConnectError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# Refresh the access token this many seconds before it expires so a long
# call doesn't 401 mid-flight.
_TOKEN_REFRESH_LEEWAY = timedelta(seconds=60)


class PipedreamConnectClient:
    """Thin async wrapper over the Pipedream Connect REST surface."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = client or httpx.AsyncClient(timeout=15.0)
        self._owned_http = client is None
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owned_http:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Connect lifecycle
    # ------------------------------------------------------------------
    async def create_connect_token(
        self,
        *,
        external_user_id: str,
        expires_in: int | None = None,
        allowed_origins: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mint a Connect token for a rep.

        Pipedream's response includes ``id``, ``token``, ``expires_at``
        and (per their managed-auth docs) a ``connect_link_url`` we can
        either use directly or augment with ``?app=<slug>`` to pre-pick
        which app the rep is connecting.
        """
        body: dict[str, Any] = {"external_user_id": external_user_id}
        if expires_in is not None:
            body["expires_in"] = expires_in
        if allowed_origins:
            body["allowed_origins"] = allowed_origins
        return await self._authed_request(
            "POST",
            f"/connect/{self._settings.alex_pipedream_connect_project_id}/tokens",
            json=body,
        )

    @staticmethod
    def build_connect_link_url(
        *,
        base_connect_link_url: str,
        app_slug: str | None = None,
    ) -> str:
        """Append ``app=<slug>`` to the URL Pipedream returned.

        Pipedream returns a complete ``connect_link_url`` but to skip the
        in-iframe app picker we tack the slug on the query string. The
        rep sees only the provider's own consent page.
        """
        if not app_slug:
            return base_connect_link_url
        sep = "&" if "?" in base_connect_link_url else "?"
        return f"{base_connect_link_url}{sep}{urlencode({'app': app_slug})}"

    async def list_accounts(
        self,
        *,
        external_user_id: str | None = None,
        app_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        """List connected accounts in the current project.

        Used by the webhook fallback path: if we miss a webhook, the
        orchestrator can poll for the rep's new account by
        ``external_user_id`` + ``app_slug``.
        """
        params: dict[str, str] = {}
        if external_user_id:
            params["external_user_id"] = external_user_id
        if app_slug:
            params["app"] = app_slug
        path = f"/connect/{self._settings.alex_pipedream_connect_project_id}/accounts"
        if params:
            path = f"{path}?{urlencode(params)}"
        result = await self._authed_request("GET", path)
        return result.get("data", []) if isinstance(result, dict) else []

    async def delete_account(self, *, account_id: str) -> None:
        path = (
            f"/connect/{self._settings.alex_pipedream_connect_project_id}"
            f"/accounts/{account_id}"
        )
        await self._authed_request("DELETE", path)

    # ------------------------------------------------------------------
    # Proxy
    # ------------------------------------------------------------------
    async def proxy_request(
        self,
        *,
        external_user_id: str,
        account_id: str,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        raw_body: bytes | None = None,
    ) -> dict[str, Any]:
        """Make a request to a provider's API via Pipedream's Connect proxy.

        Pipedream injects the rep's OAuth credentials based on the
        ``account_id`` query parameter. The response is the upstream
        provider's body, wrapped in Pipedream's envelope.
        """
        params = {
            "external_user_id": external_user_id,
            "account_id": account_id,
        }
        body: dict[str, Any] = {
            "url": url,
            "method": method.upper(),
        }
        if headers:
            body["headers"] = headers
        if json_body is not None:
            body["body"] = json_body
        elif raw_body is not None:
            body["body"] = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
        path = (
            f"/connect/{self._settings.alex_pipedream_connect_project_id}"
            f"/proxy?{urlencode(params)}"
        )
        return await self._authed_request("POST", path, json=body)

    # ------------------------------------------------------------------
    # Auth — client_credentials with caching
    # ------------------------------------------------------------------
    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            now = datetime.now(timezone.utc)
            if (
                not force_refresh
                and self._access_token
                and self._token_expires_at
                and now + _TOKEN_REFRESH_LEEWAY < self._token_expires_at
            ):
                return self._access_token

            response = await self._http.post(
                f"{self._settings.alex_pipedream_connect_api_base}/oauth/token",
                json={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.alex_pipedream_connect_client_id,
                    "client_secret": self._settings.alex_pipedream_connect_client_secret,
                },
                headers={"Content-Type": "application/json"},
            )
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if response.status_code != 200 or not isinstance(parsed, dict):
                raise PipedreamConnectError(
                    "Pipedream OAuth token exchange failed",
                    status=response.status_code,
                    body=parsed,
                )
            token = parsed.get("access_token")
            expires_in = parsed.get("expires_in", 3600)
            if not isinstance(token, str):
                raise PipedreamConnectError(
                    "Pipedream OAuth response missing access_token", body=parsed
                )
            self._access_token = token
            self._token_expires_at = now + timedelta(seconds=int(expires_in))
            log.info(
                "pipedream_connect.token_refreshed",
                expires_in=int(expires_in),
            )
            return token

    async def _authed_request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
    ) -> dict[str, Any]:
        url = f"{self._settings.alex_pipedream_connect_api_base}{path}"
        for attempt in range(2):
            token = await self._get_access_token(force_refresh=attempt > 0)
            response = await self._http.request(
                method,
                url,
                json=json,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-PD-Environment": self._settings.alex_pipedream_connect_environment,
                    "Content-Type": "application/json",
                },
            )
            if response.status_code == 401 and attempt == 0:
                # Cached token rejected — refresh once and retry.
                continue
            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            if response.status_code >= 400:
                raise PipedreamConnectError(
                    f"Pipedream Connect {method} {path} returned {response.status_code}",
                    status=response.status_code,
                    body=parsed,
                )
            return parsed if isinstance(parsed, dict) else {"raw": parsed}
        raise PipedreamConnectError(
            f"Pipedream Connect {method} {path} retried but still 401"
        )


def build_pipedream_connect_client(settings: Settings) -> PipedreamConnectClient | None:
    """Return a client iff project credentials are present in settings.

    Lets the runtime boot in stub mode without Connect creds and only
    instantiates the client when the operator has actually configured it.
    """
    if not (
        settings.alex_pipedream_connect_project_id
        and settings.alex_pipedream_connect_client_id
        and settings.alex_pipedream_connect_client_secret
    ):
        return None
    return PipedreamConnectClient(settings)
