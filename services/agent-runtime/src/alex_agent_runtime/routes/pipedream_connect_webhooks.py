"""``/webhooks/pipedream-connect`` — receive Connect account-lifecycle events.

Pipedream POSTs here whenever a rep finishes connecting (or removing) an
account via Connect. The payload is HMAC-SHA256 signed against the secret
the operator registered with the webhook in Pipedream's dashboard
(``ALEX_PIPEDREAM_CONNECT_WEBHOOK_SECRET``); we verify before doing any
state work to keep the endpoint safe to expose to the public internet.

Wire contract (from Pipedream docs)::

    X-PD-Signature: t=<unix_seconds>,v1=<sha256_hex>
    Content-Type: application/json
    {
        "event": "CONNECTION_SUCCESS",
        "connect_token": "ctok_xxx",
        "environment": "production",
        "account": {
            "id": "apn_xxx",
            "external_id": "alex:<tenant>:<rep>:<connector>",
            "healthy": true,
            "app": {"name_slug": "gmail", ...},
            ...
        }
    }

For ``CONNECTION_SUCCESS`` events whose ``external_id`` decodes to one
of our reps + an expected Pipedream app slug, we forward to the
:class:`OAuthOrchestrator` Connect-completion path. Failure events flip
the connector status to FAILED so the rep gets a Slack reconnect prompt.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..schemas import OnboardingConnector
from ..services.oauth_provider import PipedreamConnectOAuthProvider

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks")


# Pipedream's documented event names.
_EVENT_SUCCESS = "CONNECTION_SUCCESS"
_FAILURE_EVENTS = {"CONNECTION_ERROR", "ACCOUNT_REMOVED"}


# Pipedream signature header allows up to a few minutes of clock skew on
# their side; reject anything older than this to bound replay risk.
SIGNATURE_MAX_AGE_SECONDS = 300


class _PipedreamApp(BaseModel):
    name_slug: str
    name: str | None = None


class _PipedreamAccount(BaseModel):
    id: str
    name: str | None = None
    external_id: str = Field(min_length=1)
    healthy: bool = True
    app: _PipedreamApp


class _PipedreamEvent(BaseModel):
    event: str
    connect_token: str | None = None
    environment: str | None = None
    account: _PipedreamAccount

    # Pipedream may evolve the payload; ignore unknown fields rather than 422.
    model_config = {"extra": "ignore"}


@router.post("/pipedream-connect", status_code=status.HTTP_200_OK)
async def pipedream_connect_webhook(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    settings = request.app.state.settings
    signature = request.headers.get("x-pd-signature")
    if settings.alex_pipedream_connect_webhook_secret:
        _verify_signature(
            signing_key=settings.alex_pipedream_connect_webhook_secret,
            signature_header=signature,
            raw_body=raw_body,
        )
    elif signature is not None:
        # A secret hasn't been configured yet but Pipedream is signing —
        # log loudly so we don't silently accept unverified payloads in
        # production. Permitted only because the surrounding `.env` is
        # still being filled in during WO #24 setup.
        log.warning(
            "pipedream_connect_webhook.unverified",
            reason="webhook_secret_not_configured",
        )

    try:
        event = _PipedreamEvent.model_validate_json(raw_body)
    except Exception as exc:  # noqa: BLE001 — pydantic gives a clear message
        log.warning("pipedream_connect_webhook.malformed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed pipedream-connect payload",
        ) from exc

    decoded = PipedreamConnectOAuthProvider.decode_external_user_id(
        event.account.external_id
    )
    if decoded is None:
        # Event for a tenant/project Alex doesn't own. ACK without doing
        # anything so Pipedream stops retrying.
        log.info(
            "pipedream_connect_webhook.ignored_foreign_external_id",
            external_id=event.account.external_id,
            event_type=event.event,
        )
        return {"status": "ignored", "reason": "foreign_external_id"}

    tenant_id, rep_id, connector = decoded
    orchestrator = request.app.state.oauth_orchestrator
    flow = getattr(request.app.state, "onboarding_flow", None)

    app_slug = event.account.app.name_slug
    expected_slugs = PipedreamConnectOAuthProvider.APP_SLUGS.get(connector, ())
    if app_slug not in expected_slugs:
        log.info(
            "pipedream_connect_webhook.unexpected_app_slug",
            connector=connector.value,
            app_slug=app_slug,
            allowed=list(expected_slugs),
        )
        # ACK so Pipedream stops retrying, but skip state mutation —
        # this app isn't one we asked the rep to connect.
        return {"status": "ignored", "reason": "unexpected_app_slug"}

    is_primary = expected_slugs[0] == app_slug if expected_slugs else False

    if event.event == _EVENT_SUCCESS:
        completion = await orchestrator.complete_via_pipedream_connect(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            account_id=event.account.id,
            app_slug=app_slug,
            is_primary_slug=is_primary,
        )
        if completion is not None and flow is not None:
            await flow.on_connector_completed(completion=completion)
        return {
            "status": "ok",
            "connector": connector.value,
            "app_slug": app_slug,
            "primary": is_primary,
        }

    if event.event in _FAILURE_EVENTS:
        completion = await orchestrator.fail_via_pipedream_connect(
            tenant_id=tenant_id,
            rep_id=rep_id,
            connector=connector,
            reason=event.event.lower(),
        )
        if flow is not None:
            await flow.on_connector_completed(completion=completion)
        return {
            "status": "ok",
            "connector": connector.value,
            "app_slug": app_slug,
            "result": "failed",
        }

    log.info(
        "pipedream_connect_webhook.unhandled_event",
        event_type=event.event,
        connector=connector.value,
    )
    return {"status": "ignored", "reason": "unhandled_event", "event": event.event}


# ---------------------------------------------------------------------------
# HMAC verification (matches Pipedream's documented scheme)
# ---------------------------------------------------------------------------
def _verify_signature(
    *,
    signing_key: str,
    signature_header: str | None,
    raw_body: bytes,
) -> None:
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing x-pd-signature header",
        )
    try:
        parts = dict(part.split("=", 1) for part in signature_header.split(","))
        timestamp = parts["t"]
        received_sig = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed x-pd-signature header",
        ) from exc

    # Bound replay risk.
    try:
        ts_int = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid timestamp",
        ) from exc
    import time

    if abs(time.time() - ts_int) > SIGNATURE_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="signature timestamp out of range",
        )

    signed_payload = f"{timestamp}.{raw_body.decode('utf-8', errors='replace')}"
    expected_sig = hmac.new(
        signing_key.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received_sig, expected_sig):
        log.warning("pipedream_connect_webhook.bad_signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )
