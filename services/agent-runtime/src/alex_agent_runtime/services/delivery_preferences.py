"""Per-rep delivery-channel preferences backed by ``tenant_config``.

The blueprint puts these in :class:`TenantConfig`. We store them as a
single JSONB row keyed ``('delivery_preferences', tenant_id)`` with a
two-level map:

```
{
    "<rep_uuid>": {
        "<output_type>": "<channel>",
        ...
    },
    ...
}
```

Defaults fall through in this order:

1. The (rep, output_type) explicit override.
2. The rep's default channel (key ``"*"`` under their entry).
3. The tenant's default channel (key ``"*"`` at the top level).
4. The hard-coded global default: ``DeliveryChannel.SLACK``.

Writes go through :meth:`set_channel`; reads through
:meth:`get_channel`. The runtime treats these as cheap reads — there's
no caching layer yet, but the tenant_config row is small so a single
SELECT per delivery is fine for v1 volume.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..schemas import DeliveryChannel
from ..tenant_context import tenant_scope

log = structlog.get_logger(__name__)


_CONFIG_KEY = "delivery_preferences"
_WILDCARD = "*"


class DeliveryPreferenceRepo:
    """Stateless service. One instance per process is fine."""

    async def get_channel(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        output_type: str,
    ) -> DeliveryChannel:
        prefs = await self._load(tenant_id)
        channel = (
            _maybe_channel(_nested(prefs, str(rep_id), output_type))
            or _maybe_channel(_nested(prefs, str(rep_id), _WILDCARD))
            or _maybe_channel(_nested(prefs, _WILDCARD))
            or DeliveryChannel.SLACK
        )
        log.debug(
            "delivery_preferences.lookup",
            tenant_id=str(tenant_id),
            rep_id=str(rep_id),
            output_type=output_type,
            channel=channel.value,
        )
        return channel

    async def set_channel(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        output_type: str,
        channel: DeliveryChannel,
    ) -> None:
        prefs = await self._load(tenant_id)
        rep_key = str(rep_id)
        rep_entry = prefs.get(rep_key)
        if not isinstance(rep_entry, dict):
            rep_entry = {}
        rep_entry[output_type] = channel.value
        prefs[rep_key] = rep_entry
        await self._save(tenant_id, prefs)

    async def _load(self, tenant_id: UUID) -> dict[str, Any]:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        SELECT value
                          FROM tenant_config
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND key = :key
                        """
                    ),
                    {"key": _CONFIG_KEY},
                )
                record = row.mappings().one_or_none()
        if record is None:
            return {}
        value = record["value"]
        return value if isinstance(value, dict) else {}

    async def _save(self, tenant_id: UUID, prefs: dict[str, Any]) -> None:
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
                    {"key": _CONFIG_KEY, "value": json.dumps(prefs)},
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _nested(d: dict[str, Any], *keys: str) -> Any:
    cursor: Any = d
    for key in keys:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


def _maybe_channel(value: Any) -> DeliveryChannel | None:
    if isinstance(value, str) and value in DeliveryChannel._value2member_map_:
        return DeliveryChannel(value)
    return None
