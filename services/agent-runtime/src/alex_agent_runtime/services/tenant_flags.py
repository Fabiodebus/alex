"""Per-tenant feature flags read from ``tenant_config``.

Each flag lives as its own ``tenant_config`` row with the literal key
``flag:<name>`` and a JSONB value ``{"enabled": true|false}``. Keeping
flags in their own rows (vs one combined map) lets two flags be
toggled independently without read-modify-write races.

For now we have one flag — ``meddic_enabled`` — gating WO #19's
MEDDICMapper. Future flags slot in via the same pattern.
"""
from __future__ import annotations

import json
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..tenant_context import tenant_scope

log = structlog.get_logger(__name__)


_KEY_PREFIX = "flag:"
FLAG_MEDDIC_ENABLED = "meddic_enabled"


class TenantFlagRepo:
    async def get_bool(
        self,
        *,
        tenant_id: UUID,
        flag: str,
        default: bool = False,
    ) -> bool:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        SELECT value FROM tenant_config
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND key = :key
                        """
                    ),
                    {"key": f"{_KEY_PREFIX}{flag}"},
                )
                record = row.mappings().one_or_none()
        if record is None:
            return default
        value = record["value"]
        if isinstance(value, dict) and "enabled" in value:
            raw = value["enabled"]
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.lower() in {"true", "yes", "1"}
        return default

    async def set_bool(
        self,
        *,
        tenant_id: UUID,
        flag: str,
        enabled: bool,
    ) -> None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO tenant_config (tenant_id, key, value)
                        VALUES (
                            current_setting('app.tenant_id')::uuid,
                            :key,
                            CAST(:value AS jsonb)
                        )
                        ON CONFLICT (tenant_id, key) DO UPDATE
                          SET value = EXCLUDED.value,
                              updated_at = now()
                        """
                    ),
                    {
                        "key": f"{_KEY_PREFIX}{flag}",
                        "value": json.dumps({"enabled": enabled}),
                    },
                )
        log.info(
            "tenant_flags.set",
            tenant_id=str(tenant_id),
            flag=flag,
            enabled=enabled,
        )
