"""Liveness heartbeat. Feature jobs replace or augment this."""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


async def heartbeat() -> None:
    log.info("scheduler.heartbeat")
