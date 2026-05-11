"""ConnectionStatus API.

POST /connections/status  ← from the Pipedream OAuth relay workflow.
GET  /connections/{source}?rep_id=...  ← used by feature workflows to
   short-circuit when a rep hasn't yet connected the required source.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from ..schemas import ConnectionStatusUpdate, ConnectionStatusView
from ..services.connection_repo import get_connection, upsert_connection

router = APIRouter(prefix="/connections")


@router.post("/status", response_model=ConnectionStatusView, status_code=status.HTTP_200_OK)
async def post_status(update: ConnectionStatusUpdate) -> ConnectionStatusView:
    return await upsert_connection(update)


@router.get("/{source}", response_model=ConnectionStatusView)
async def get_status(
    source: str,
    tenant_id: UUID = Query(..., description="Tenant uuid (must match X-Tenant-Id)"),
    rep_id: UUID = Query(..., description="Rep uuid"),
) -> ConnectionStatusView:
    view = await get_connection(tenant_id=tenant_id, rep_id=rep_id, source=source)
    if view is None:
        raise HTTPException(status_code=404, detail="connection not found")
    return view
