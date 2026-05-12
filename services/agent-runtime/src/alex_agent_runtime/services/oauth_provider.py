"""OAuth provider abstractions for the Onboarding capability.

Two implementations:

* :class:`StubOAuthProvider` — default in dev/test. Generates a
  synthetic CSRF state, returns a self-served "stub authorize URL"
  the Slack bot can short-circuit, and on ``exchange`` produces a
  synthetic ``token_ref`` so the orchestrator can advance state.
  Lets the entire onboarding sequence be walked end-to-end without
  real OAuth credentials.

* :class:`PipedreamOAuthProvider` — production. POSTs to a per-
  connector Pipedream workflow URL to start the real OAuth dance;
  the Pipedream side handles the provider's redirect and the
  Messaging Surface OAuth redirect handler delivers the eventual
  token to the runtime's callback endpoint.

Switch via ``Settings.oauth_provider`` (``"stub"`` vs ``"pipedream"``).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable
from uuid import UUID

import httpx
import structlog

from ..config import Settings, get_settings
from ..schemas import OAuthInitiation, OnboardingConnector
from .pipedream_client import _sign
from .pipedream_connect_client import (
    PipedreamConnectClient,
    PipedreamConnectError,
    build_pipedream_connect_client,
)

log = structlog.get_logger(__name__)


# How long a started OAuth state is valid before the rep has to re-click
# the "Connect" button. 15 minutes matches the onboarding-target window.
OAUTH_STATE_TTL = timedelta(minutes=15)


class OAuthProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: object | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@runtime_checkable
class OAuthProvider(Protocol):
    name: str

    async def initiate(
        self,
        *,
        connector: OnboardingConnector,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> OAuthInitiation: ...

    async def exchange(
        self,
        *,
        connector: OnboardingConnector,
        state: str,
        code: str | None,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> dict[str, object]: ...


class StubOAuthProvider:
    """Synthesises a successful OAuth round-trip locally."""

    name = "stub"

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    async def initiate(
        self,
        *,
        connector: OnboardingConnector,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> OAuthInitiation:
        state = _new_state()
        # The "authorize_url" for stub mode points back at the runtime's
        # own onboarding callback so the Slack bot button flow stays
        # self-contained. The state is the dedup key.
        base = self._settings.alex_agent_runtime_public_url.rstrip("/") if (
            self._settings.alex_agent_runtime_public_url
        ) else ""
        url = (
            f"{base}/onboarding/oauth/stub_complete"
            f"?connector={connector.value}&state={state}&rep_id={rep_id}"
        )
        log.info(
            "oauth_provider.stub.initiate",
            connector=connector.value,
            rep_id=str(rep_id),
            state=state,
        )
        return OAuthInitiation(
            connector=connector,
            state=state,
            authorize_url=url,
            stub=True,
            expires_at=_expires_at(),
        )

    async def exchange(
        self,
        *,
        connector: OnboardingConnector,
        state: str,
        code: str | None,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> dict[str, object]:
        # Synthetic token; the real exchange would call the provider's
        # token endpoint. The ``token_ref`` is what the orchestrator
        # forwards to the Pipedream vault (or in stub mode, what gets
        # recorded against connection_repo for later visibility).
        token_ref = f"stub://{connector.value}/{rep_id}"
        log.info(
            "oauth_provider.stub.exchange",
            connector=connector.value,
            rep_id=str(rep_id),
            state=state,
        )
        return {
            "connector": connector.value,
            "token_ref": token_ref,
            "scopes": _stub_scopes(connector),
        }


class PipedreamOAuthProvider:
    """Real-OAuth path via Pipedream workflows.

    For each connector the runtime POSTs to a connector-specific
    workflow URL (e.g. ``ALEX_PIPEDREAM_OAUTH_CLOSE_URL``) that returns
    ``{"authorize_url", "state"}``. The Messaging Surface OAuth
    redirect handler later receives the provider's callback and
    forwards ``{state, code}`` to the runtime's
    ``/onboarding/oauth/callback`` endpoint, which calls
    :meth:`exchange` to swap the code for tokens via the same workflow.
    """

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

    async def initiate(
        self,
        *,
        connector: OnboardingConnector,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> OAuthInitiation:
        url = self._url_for(connector, action="initiate")
        data = await self._post(
            url,
            body={
                "connector": connector.value,
                "tenant_id": str(tenant_id),
                "rep_id": str(rep_id),
                "action": "initiate",
            },
            tenant_id=tenant_id,
        )
        authorize_url = data.get("authorize_url")
        state = data.get("state")
        if not isinstance(authorize_url, str) or not isinstance(state, str):
            raise OAuthProviderError(
                f"Pipedream OAuth initiate for {connector.value} returned malformed payload",
                body=data,
            )
        return OAuthInitiation(
            connector=connector,
            state=state,
            authorize_url=authorize_url,
            stub=False,
            expires_at=_expires_at(),
        )

    async def exchange(
        self,
        *,
        connector: OnboardingConnector,
        state: str,
        code: str | None,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> dict[str, object]:
        url = self._url_for(connector, action="exchange")
        data = await self._post(
            url,
            body={
                "connector": connector.value,
                "tenant_id": str(tenant_id),
                "rep_id": str(rep_id),
                "state": state,
                "code": code,
                "action": "exchange",
            },
            tenant_id=tenant_id,
        )
        if not isinstance(data, dict) or "token_ref" not in data:
            raise OAuthProviderError(
                f"Pipedream OAuth exchange for {connector.value} returned malformed payload",
                body=data,
            )
        return data

    async def _post(
        self,
        url: str,
        *,
        body: dict[str, object],
        tenant_id: UUID,
    ) -> dict[str, object]:
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": str(tenant_id),
            "X-Alex-Timestamp": timestamp,
        }
        if self._settings.alex_webhook_secret:
            headers["X-Alex-Signature"] = _sign(
                self._settings.alex_webhook_secret, timestamp, body_bytes.decode("utf-8")
            )
        response = await self._http.post(url, content=body_bytes, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            raise OAuthProviderError(
                f"Pipedream OAuth returned {response.status_code}",
                status=response.status_code,
                body=parsed,
            )
        if not isinstance(parsed, dict):
            return {"raw": parsed}
        return parsed

    def _url_for(self, connector: OnboardingConnector, *, action: str) -> str:
        urls = {
            OnboardingConnector.CLOSE: self._settings.alex_pipedream_oauth_close_url,
            OnboardingConnector.GOOGLE: self._settings.alex_pipedream_oauth_google_url,
            OnboardingConnector.KRISP: self._settings.alex_pipedream_oauth_krisp_url,
        }
        url = urls.get(connector, "")
        if not url:
            raise OAuthProviderError(
                f"No Pipedream OAuth URL configured for {connector.value}"
            )
        return url


class PipedreamConnectOAuthProvider:
    """Real-OAuth path via Pipedream Connect (WO #24).

    Pipedream owns the OAuth client; we mint a short-lived Connect token
    per (tenant, rep, connector) and hand the rep a connect-link URL.
    When the rep finishes authorising the upstream provider, Pipedream
    POSTs an ``account.created`` webhook to ``/webhooks/pipedream-connect``
    with the resulting ``account.id``. That webhook handler — not this
    provider — drives connection completion. ``exchange`` is therefore
    unreachable in the Connect flow and raises if called.

    ``OnboardingConnector.GOOGLE`` maps to two underlying Pipedream apps
    (``gmail`` + ``google_calendar``). For v1 we connect Gmail first
    (the dependency that unblocks email send). Calendar is wired by the
    webhook handler once Gmail lands — same connector flag, two account
    rows under the hood.
    """

    name = "pipedream_connect"

    # Each connector maps to one or more Pipedream app slugs. The first
    # entry is the slug that goes into the initial connect-link URL; the
    # rest are auto-prompted once the first one lands.
    APP_SLUGS: dict[OnboardingConnector, tuple[str, ...]] = {
        OnboardingConnector.CLOSE: ("close",),
        OnboardingConnector.GOOGLE: ("gmail", "google_calendar"),
        # Krisp transcript ingest doesn't use Pipedream Connect — falls
        # back to direct webhook ingress.
    }

    def __init__(
        self,
        settings: Settings,
        *,
        client: PipedreamConnectClient | None = None,
    ) -> None:
        self._settings = settings
        owned = client is None
        if client is None:
            client = build_pipedream_connect_client(settings)
        if client is None:
            raise OAuthProviderError(
                "PipedreamConnectOAuthProvider requires "
                "ALEX_PIPEDREAM_CONNECT_PROJECT_ID + CLIENT_ID + CLIENT_SECRET to be set"
            )
        self._client = client
        self._owned_client = owned

    async def close(self) -> None:
        if self._owned_client:
            await self._client.close()

    @classmethod
    def primary_app_slug(cls, connector: OnboardingConnector) -> str | None:
        """First app slug we'll prompt for. Webhook completes the rest."""
        slugs = cls.APP_SLUGS.get(connector)
        return slugs[0] if slugs else None

    @classmethod
    def follow_up_app_slugs(cls, connector: OnboardingConnector) -> tuple[str, ...]:
        """Apps to chain after the primary one (e.g. Calendar after Gmail)."""
        slugs = cls.APP_SLUGS.get(connector)
        return slugs[1:] if slugs and len(slugs) > 1 else ()

    @staticmethod
    def encode_external_user_id(
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
    ) -> str:
        """Format Pipedream returns to us via webhook (≤ 250 chars).

        Pipedream stores ``external_user_id`` per account and echoes it
        back on every event. Encoding the connector here lets the webhook
        handler resolve which Alex connector each Pipedream app maps to,
        even when one rep has both Gmail and Calendar connected.
        """
        return f"alex:{tenant_id}:{rep_id}:{connector.value}"

    @staticmethod
    def decode_external_user_id(
        value: str,
    ) -> tuple[UUID, UUID, OnboardingConnector] | None:
        """Inverse of ``encode_external_user_id``. Returns ``None`` if the
        payload didn't originate from us (e.g. another tenant or a
        misconfigured Pipedream project)."""
        parts = value.split(":", 3)
        if len(parts) != 4 or parts[0] != "alex":
            return None
        try:
            tenant_id = UUID(parts[1])
            rep_id = UUID(parts[2])
            connector = OnboardingConnector(parts[3])
        except (ValueError, KeyError):
            return None
        return tenant_id, rep_id, connector

    async def initiate(
        self,
        *,
        connector: OnboardingConnector,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> OAuthInitiation:
        app_slug = self.primary_app_slug(connector)
        if app_slug is None:
            raise OAuthProviderError(
                f"Pipedream Connect has no app mapping for connector {connector.value}"
            )
        external_user_id = self.encode_external_user_id(
            tenant_id=tenant_id, rep_id=rep_id, connector=connector
        )
        try:
            data = await self._client.create_connect_token(
                external_user_id=external_user_id,
                expires_in=int(OAUTH_STATE_TTL.total_seconds()),
            )
        except PipedreamConnectError as exc:
            raise OAuthProviderError(
                f"Pipedream Connect token creation failed: {exc}",
                status=exc.status,
                body=exc.body,
            ) from exc
        token = data.get("token")
        connect_link_url = data.get("connect_link_url")
        if not isinstance(token, str) or not isinstance(connect_link_url, str):
            raise OAuthProviderError(
                "Pipedream Connect returned a malformed token payload",
                body=data,
            )
        authorize_url = PipedreamConnectClient.build_connect_link_url(
            base_connect_link_url=connect_link_url,
            app_slug=app_slug,
        )
        log.info(
            "oauth_provider.pipedream_connect.initiate",
            connector=connector.value,
            rep_id=str(rep_id),
            app_slug=app_slug,
            token_id=data.get("id"),
        )
        return OAuthInitiation(
            connector=connector,
            state=token,  # the Pipedream Connect token doubles as our CSRF state
            authorize_url=authorize_url,
            stub=False,
            expires_at=_expires_at(),
        )

    async def exchange(
        self,
        *,
        connector: OnboardingConnector,
        state: str,
        code: str | None,
        tenant_id: UUID,
        rep_id: UUID,
    ) -> dict[str, object]:
        # Pipedream Connect does not surface an authorization code back
        # to us — completion is webhook-driven. Anything reaching this
        # method is a misconfiguration (e.g. someone wired the legacy
        # /onboarding/oauth/callback against Connect mode).
        raise OAuthProviderError(
            "PipedreamConnectOAuthProvider.exchange called — "
            "webhook receiver should drive completion in this mode"
        )


def build_default_oauth_provider(settings: Settings | None = None) -> OAuthProvider:
    s = settings or get_settings()
    if s.oauth_provider == "pipedream_connect":
        log.info("oauth_provider.selected", provider="pipedream_connect")
        return PipedreamConnectOAuthProvider(s)
    if s.oauth_provider == "pipedream":
        log.info("oauth_provider.selected", provider="pipedream")
        return PipedreamOAuthProvider(s)
    log.warning("oauth_provider.selected", provider="stub")
    return StubOAuthProvider(settings=s)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _new_state() -> str:
    return secrets.token_urlsafe(24)


def _expires_at() -> datetime:
    return datetime.now(timezone.utc) + OAUTH_STATE_TTL


def _stub_scopes(connector: OnboardingConnector) -> list[str]:
    return {
        OnboardingConnector.CLOSE: ["crm.read", "crm.write"],
        OnboardingConnector.GOOGLE: [
            "gmail.read",
            "gmail.send",
            "calendar.read",
        ],
        OnboardingConnector.KRISP: ["meetings.read"],
    }.get(connector, [])
