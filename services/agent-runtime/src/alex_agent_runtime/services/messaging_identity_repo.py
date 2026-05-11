"""Lookups over the ``messaging_identities`` table.

The :class:`HttpMessagingDeliveryClient` consults this repo when it
needs to resolve a runtime ``rep_id`` to a surface-specific
identifier (e.g. Slack ``external_user_id``) before POSTing to the
messaging surface's ``/deliver`` endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import transactional_session
from ..tenant_context import tenant_scope

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SlackIdentity:
    external_user_id: str
    dm_channel_id: str | None = None


class MessagingIdentityRepo:
    async def get_slack_identity(
        self, *, tenant_id: UUID, rep_id: UUID
    ) -> SlackIdentity | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        SELECT external_user_id, dm_channel_id
                          FROM messaging_identities
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND platform = 'slack'
                           AND rep_id = :rep_id
                        """
                    ),
                    {"rep_id": rep_id},
                )
                record = row.mappings().one_or_none()
        if record is None:
            return None
        return SlackIdentity(
            external_user_id=record["external_user_id"],
            dm_channel_id=record["dm_channel_id"],
        )
