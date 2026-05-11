"""Liveness and readiness probes."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ..db import session_factory

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    factory = session_factory()
    async with factory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ready"}
