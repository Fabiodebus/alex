"""OAuth start + callback endpoints. Identical contract to the slack-bot."""
from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..schemas import OAuthToken
from ..services.oauth_providers import (
    InvalidStateError,
    UnknownProviderError,
    build_auth_url,
    exchange_code,
)
from ..services.pipedream_client import PipedreamOAuthClient, PipedreamOAuthError

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/oauth")


@router.get("/start")
async def oauth_start(
    provider: str = Query(...),
    tenant_id: UUID = Query(...),
    rep_id: UUID = Query(...),
) -> RedirectResponse:
    try:
        url = build_auth_url(provider=provider, tenant_id=tenant_id, rep_id=rep_id)
    except UnknownProviderError as exc:
        raise HTTPException(status_code=400, detail={"error": "unknown_provider", "provider": provider}) from exc
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    provider: str = Query(default="google"),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    if error:
        raise HTTPException(
            status_code=400,
            detail={"error": "oauth_provider_error", "provider_error": error},
        )
    try:
        exchanged = await exchange_code(provider=provider, code=code, state=state)
    except InvalidStateError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_state", "detail": str(exc)}) from exc
    except UnknownProviderError as exc:
        raise HTTPException(status_code=400, detail={"error": "unknown_provider"}) from exc

    token = OAuthToken.model_validate(exchanged["token"])
    pipedream_client: PipedreamOAuthClient = request.app.state.pipedream_oauth_client
    try:
        await pipedream_client.relay(token)
    except PipedreamOAuthError as exc:
        log.warning("oauth_callback.relay_failed", status=exc.status, body=exc.body)
        raise HTTPException(status_code=502, detail={"error": "oauth_relay_failed", "status": exc.status}) from exc

    log.info(
        "oauth_callback.success",
        provider=provider,
        tenant_id=str(token.tenant_id),
        rep_id=str(token.rep_id),
    )
    return HTMLResponse(
        content=(
            "<html><head><title>Connected</title></head>"
            "<body style='font-family: -apple-system, sans-serif; padding: 2em;'>"
            f"<h2>Connected {provider.title()}.</h2>"
            "<p>You can close this tab and return to Teams.</p>"
            "</body></html>"
        ),
        status_code=status.HTTP_200_OK,
    )
