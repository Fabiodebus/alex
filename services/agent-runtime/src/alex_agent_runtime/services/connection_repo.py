"""oauth_connections read/write helpers.

Owned by the Agent Runtime — Pipedream is the source of truth for the
encrypted token material, but this table is the runtime's view of which
connections are active per (tenant, rep, source) and is what feature
workflows query before dispatching ActionRequests.
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text

from ..db import transactional_session
from ..schemas import ConnectionStatus, ConnectionStatusUpdate, ConnectionStatusView
from ..tenant_context import tenant_scope


async def upsert_connection(update: ConnectionStatusUpdate) -> ConnectionStatusView:
    """Insert a new connection or refresh an existing one."""
    with tenant_scope(update.tenant_id):
        async with transactional_session() as session:
            row = await session.execute(
                text(
                    """
                    INSERT INTO oauth_connections
                        (tenant_id, rep_id, source, status, scopes, vault_ref, connected_at, last_seen_at)
                    VALUES
                        (current_setting('app.tenant_id')::uuid,
                         :rep_id, :source, :status,
                         CAST(:scopes AS jsonb), :vault_ref,
                         now(), now())
                    ON CONFLICT (tenant_id, rep_id, source) DO UPDATE
                       SET status = EXCLUDED.status,
                           scopes = EXCLUDED.scopes,
                           vault_ref = COALESCE(EXCLUDED.vault_ref, oauth_connections.vault_ref),
                           last_seen_at = now()
                    RETURNING tenant_id, rep_id, source, status, scopes, connected_at, last_seen_at
                    """
                ),
                {
                    "rep_id": str(update.rep_id),
                    "source": update.source,
                    "status": update.status.value,
                    "scopes": json.dumps(list(update.scopes)),
                    "vault_ref": update.vault_ref,
                },
            )
            r = row.one()
            return ConnectionStatusView(
                tenant_id=r.tenant_id,
                rep_id=r.rep_id,
                source=r.source,
                status=ConnectionStatus(r.status),
                scopes=list(r.scopes),
                connected_at=r.connected_at,
                last_seen_at=r.last_seen_at,
            )


async def get_connection(
    *, tenant_id: UUID, rep_id: UUID, source: str
) -> ConnectionStatusView | None:
    with tenant_scope(tenant_id):
        async with transactional_session() as session:
            row = await session.execute(
                text(
                    """
                    SELECT tenant_id, rep_id, source, status, scopes, connected_at, last_seen_at
                      FROM oauth_connections
                     WHERE rep_id = :rep_id AND source = :source
                    """
                ),
                {"rep_id": str(rep_id), "source": source},
            )
            r = row.one_or_none()
            if r is None:
                return None
            return ConnectionStatusView(
                tenant_id=r.tenant_id,
                rep_id=r.rep_id,
                source=r.source,
                status=ConnectionStatus(r.status),
                scopes=list(r.scopes),
                connected_at=r.connected_at,
                last_seen_at=r.last_seen_at,
            )
