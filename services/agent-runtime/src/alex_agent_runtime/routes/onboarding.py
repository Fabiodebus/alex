"""``/onboarding`` routes — start the conversation + handle OAuth callbacks.

Called by the Slack bot (when the rep runs ``/alex onboard`` or clicks
a Block-Kit button) and by the Messaging Surface OAuth redirect
handler (when the provider POSTs the auth code back).

Stub mode short-circuits via :meth:`stub_complete` so the rep can walk
the full sequence without ever leaving Slack.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import transactional_session
from ..schemas import OAuthCompletion, OnboardingConnector
from ..tenant_context import current_tenant_or_none, tenant_scope

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/onboarding")


class StartRequest(BaseModel):
    rep_id: UUID


class StartForSlackUserRequest(BaseModel):
    """Helper for the Slack bot's ``/alex onboard`` slash command.

    Resolves (or provisions) the messaging_identity row + rep, then
    kicks off the onboarding flow. Keeps Slack-specific identity
    resolution out of the bot — the bot only needs to forward the
    Slack user/team ids."""

    slack_user_id: str = Field(min_length=1)
    slack_team_id: str | None = None
    slack_display_name: str | None = None
    slack_email: str | None = None


class InitiateOAuthRequest(BaseModel):
    rep_id: UUID
    connector: OnboardingConnector


class SkipConnectorRequest(BaseModel):
    rep_id: UUID
    connector: OnboardingConnector


class OAuthCallbackRequest(BaseModel):
    rep_id: UUID
    state: str = Field(min_length=1)
    code: str | None = None


@router.post("/start", status_code=status.HTTP_200_OK)
async def start_onboarding(payload: StartRequest, request: Request) -> dict[str, object]:
    flow = request.app.state.onboarding_flow
    tenant_id = _require_tenant(request)
    state = await flow.start(tenant_id=tenant_id, rep_id=payload.rep_id)
    return _state_response(state)


@router.post("/start_for_slack_user", status_code=status.HTTP_200_OK)
async def start_for_slack_user(
    payload: StartForSlackUserRequest, request: Request
) -> dict[str, object]:
    flow = request.app.state.onboarding_flow
    tenant_id = _require_tenant(request)
    rep_id = await _resolve_or_provision_rep_for_slack(
        tenant_id=tenant_id,
        slack_user_id=payload.slack_user_id,
        slack_team_id=payload.slack_team_id,
        display_name=payload.slack_display_name,
        email=payload.slack_email,
    )
    state = await flow.start(tenant_id=tenant_id, rep_id=rep_id)
    response = _state_response(state)
    response["rep_id"] = str(rep_id)
    return response


@router.post("/oauth/initiate", status_code=status.HTTP_200_OK)
async def initiate_oauth(
    payload: InitiateOAuthRequest, request: Request
) -> dict[str, object]:
    orchestrator = request.app.state.oauth_orchestrator
    tenant_id = _require_tenant(request)
    initiation = await orchestrator.initiate(
        tenant_id=tenant_id, rep_id=payload.rep_id, connector=payload.connector
    )
    return {
        "connector": initiation.connector.value,
        "state": initiation.state,
        "authorize_url": initiation.authorize_url,
        "stub": initiation.stub,
        "expires_at": initiation.expires_at.isoformat(),
    }


@router.post("/oauth/callback", status_code=status.HTTP_200_OK)
async def oauth_callback(
    payload: OAuthCallbackRequest, request: Request
) -> dict[str, object]:
    completion = await _handle_callback(
        request=request,
        rep_id=payload.rep_id,
        state=payload.state,
        code=payload.code,
    )
    return _completion_response(completion)


@router.get("/oauth/stub_complete", status_code=status.HTTP_200_OK)
async def stub_complete(
    request: Request,
    rep_id: UUID = Query(...),
    state: str = Query(...),
    connector: str = Query(...),  # informational; the state token is the auth
) -> dict[str, object]:
    """Stub-mode shortcut.

    The Slack bot can fetch this URL on the rep's behalf (in stub mode)
    when the user clicks "Connect" — no browser redirect needed. In
    production, this endpoint is unused (the real provider redirect
    lands on ``/onboarding/oauth/callback`` instead).
    """
    if not request.app.state.oauth_orchestrator._provider.name.startswith("stub"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="stub_complete is only available when oauth_provider=stub",
        )
    completion = await _handle_callback(
        request=request, rep_id=rep_id, state=state, code=None
    )
    return _completion_response(completion)


@router.post("/skip", status_code=status.HTTP_200_OK)
async def skip_connector(
    payload: SkipConnectorRequest, request: Request
) -> dict[str, object]:
    flow = request.app.state.onboarding_flow
    tenant_id = _require_tenant(request)
    state = await flow.skip_connector(
        tenant_id=tenant_id, rep_id=payload.rep_id, connector=payload.connector
    )
    return {
        "rep_id": str(state.rep_id),
        "current_step": state.current_step.value,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _handle_callback(
    *,
    request: Request,
    rep_id: UUID,
    state: str,
    code: str | None,
) -> OAuthCompletion:
    orchestrator = request.app.state.oauth_orchestrator
    flow = request.app.state.onboarding_flow
    tenant_id = _require_tenant(request)
    completion = await orchestrator.handle_callback(
        tenant_id=tenant_id, rep_id=rep_id, state=state, code=code
    )
    await flow.on_connector_completed(completion=completion)
    return completion


def _completion_response(completion: OAuthCompletion) -> dict[str, object]:
    return {
        "connector": completion.connector.value,
        "success": completion.success,
        "token_ref": completion.token_ref,
        "failure_reason": completion.failure_reason,
    }


def _require_tenant(request: Request) -> UUID:
    """The TenantHeaderMiddleware binds the tenant via contextvar."""
    tenant_id = current_tenant_or_none()
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="X-Tenant-Id header missing"
        )
    # `request` is intentionally accepted but not introspected — kept on
    # the signature for future use (e.g. correlation IDs).
    _ = request
    return tenant_id


def _state_response(state) -> dict[str, object]:
    return {
        "rep_id": str(state.rep_id),
        "current_step": state.current_step.value,
        "ingestion_complete_at": (
            state.ingestion_complete_at.isoformat() if state.ingestion_complete_at else None
        ),
        "started_at": state.started_at.isoformat() if state.started_at else None,
    }


async def _resolve_or_provision_rep_for_slack(
    *,
    tenant_id: UUID,
    slack_user_id: str,
    slack_team_id: str | None,
    display_name: str | None,
    email: str | None,
) -> UUID:
    """Find the rep_id for a Slack identity, creating both rep +
    messaging_identity rows if they don't yet exist.

    Auto-provisioning makes the demo flow ("install the app, type
    /alex onboard") work without a separate admin step. In a stricter
    deployment the install handshake creates the rows; this fallback
    is harmless either way."""
    with tenant_scope(tenant_id):
        async with transactional_session() as session:
            row = await session.execute(
                text(
                    """
                    SELECT rep_id
                      FROM messaging_identities
                     WHERE tenant_id = current_setting('app.tenant_id')::uuid
                       AND platform = 'slack'
                       AND external_user_id = :user_id
                    """
                ),
                {"user_id": slack_user_id},
            )
            record = row.mappings().one_or_none()
            if record is not None:
                return record["rep_id"]

            new_rep_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO reps (id, tenant_id, email, display_name)
                    VALUES (:id, current_setting('app.tenant_id')::uuid,
                            :email, :display_name)
                    """
                ),
                {
                    "id": str(new_rep_id),
                    "email": email or f"slack-{slack_user_id}@unknown.local",
                    "display_name": display_name or f"slack:{slack_user_id}",
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO messaging_identities (
                        tenant_id, rep_id, platform, external_user_id, external_team_id
                    ) VALUES (
                        current_setting('app.tenant_id')::uuid,
                        :rep_id, 'slack', :external_user_id, :external_team_id
                    )
                    """
                ),
                {
                    "rep_id": str(new_rep_id),
                    "external_user_id": slack_user_id,
                    "external_team_id": slack_team_id,
                },
            )
            log.info(
                "onboarding.auto_provisioned_rep",
                rep_id=str(new_rep_id),
                slack_user_id=slack_user_id,
            )
            return new_rep_id
