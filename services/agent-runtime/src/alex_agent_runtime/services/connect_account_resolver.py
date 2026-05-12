"""Resolve a Pipedream Connect ``account_id`` for (tenant, app).

Each rep who completes the Connect flow has a row in
``onboarding_state.connector_status[<connector>]`` whose ``token_ref``
is shaped ``pdc://<pipedream_account_id>``. When Alex needs to make an
outbound call on behalf of a rep we ask this resolver to find the
right account id.

v1 strategy — for single-rep tenants we pick the first CONNECTED rep
with the requested connector. Multi-rep tenants will need a
context-aware resolver (the caller passes the originating rep_id) —
that's a deferred concern.
"""
from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..schemas import OnboardingConnector
from ..tenant_context import tenant_scope

log = structlog.get_logger(__name__)


class ConnectAccountResolver(Protocol):
    """Resolves a Pipedream Connect ``account_id`` for a (tenant, connector)."""

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        connector: OnboardingConnector,
        rep_id: UUID | None = None,
    ) -> str | None: ...


# Apps that share an underlying Pipedream Connect account with a higher-
# level OAuth connector. Used when callers know an app slug rather than
# our connector enum (e.g. Calendar wants ``google_calendar`` which is a
# follow-up app under the ``google`` connector). For now Close + Gmail
# map 1:1 to their connector.
_CONNECTOR_FOR_APP_SLUG: dict[str, OnboardingConnector] = {
    "close": OnboardingConnector.CLOSE,
    "gmail": OnboardingConnector.GOOGLE,
    "google_calendar": OnboardingConnector.GOOGLE,
}


def connector_for_app_slug(app_slug: str) -> OnboardingConnector | None:
    return _CONNECTOR_FOR_APP_SLUG.get(app_slug)


def parse_pdc_token_ref(token_ref: str | None) -> str | None:
    """Strip the ``pdc://`` prefix from a Pipedream Connect token_ref.

    Returns ``None`` when the input doesn't have the prefix (legacy
    ``stub://...`` rows, etc.).
    """
    if not isinstance(token_ref, str) or not token_ref.startswith("pdc://"):
        return None
    return token_ref[len("pdc://"):]


class DatabaseConnectAccountResolver:
    """Default resolver — reads ``onboarding_state.connector_status``."""

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        connector: OnboardingConnector,
        rep_id: UUID | None = None,
    ) -> str | None:
        # Query strategy:
        # - When rep_id is provided, look up *that* rep's row exactly.
        # - Otherwise return the first CONNECTED row in the tenant for
        #   this connector — fine for single-rep tenants which is the
        #   shape of every design partner today.
        params: dict[str, object] = {"connector": connector.value}
        sql_filter = ""
        if rep_id is not None:
            params["rep_id"] = rep_id
            sql_filter = " AND rep_id = :rep_id"

        sql = (
            "SELECT rep_id, connector_status FROM onboarding_state "
            "WHERE tenant_id = current_setting('app.tenant_id')::uuid"
            + sql_filter
            + " ORDER BY started_at ASC"
        )

        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                rows = (await session.execute(text(sql), params)).mappings().all()

        for row in rows:
            connector_status = row.get("connector_status") or {}
            if isinstance(connector_status, str):
                connector_status = json.loads(connector_status)
            slice_ = connector_status.get(connector.value) or {}
            if slice_.get("status") != "connected":
                continue
            account_id = parse_pdc_token_ref(slice_.get("token_ref"))
            if account_id:
                log.debug(
                    "connect_account_resolver.matched",
                    tenant_id=str(tenant_id),
                    rep_id=str(row.get("rep_id")),
                    connector=connector.value,
                )
                return account_id

        log.info(
            "connect_account_resolver.miss",
            tenant_id=str(tenant_id),
            connector=connector.value,
            rep_id=str(rep_id) if rep_id else None,
        )
        return None


class StaticConnectAccountResolver:
    """Test resolver — preloaded with ``(tenant, connector) -> account_id``."""

    def __init__(self, mapping: dict[tuple[UUID, OnboardingConnector], str]) -> None:
        self._mapping = mapping

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        connector: OnboardingConnector,
        rep_id: UUID | None = None,
    ) -> str | None:
        return self._mapping.get((tenant_id, connector))
