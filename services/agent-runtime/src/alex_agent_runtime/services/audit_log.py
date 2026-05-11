"""Audit log write helper.

Every approved external action — emails sent, CRM writes, deal rooms
shared — must land in the ``audit_log`` table before the action is
executed. The Data Layer enforces append-only semantics; this module
provides the single insert path so the runtime cannot accidentally bypass
the audit trail.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..schemas import AuditLogEntry


async def record_action(session: AsyncSession, entry: AuditLogEntry) -> UUID:
    """Insert a single audit_log row using the bound session's tenant.

    Returns the new row id. The caller is responsible for the surrounding
    transaction — typically the same transaction that performs the
    state change that the audit log refers to.
    """
    row = await session.execute(
        text(
            """
            INSERT INTO audit_log (
                tenant_id,
                actor_rep_id,
                approver_rep_id,
                action_type,
                target_type,
                target_id,
                prompt,
                output,
                metadata
            )
            VALUES (
                current_setting('app.tenant_id')::uuid,
                :actor_rep_id,
                :approver_rep_id,
                :action_type,
                :target_type,
                :target_id,
                CAST(:prompt AS jsonb),
                CAST(:output AS jsonb),
                CAST(:metadata AS jsonb)
            )
            RETURNING id
            """
        ),
        {
            "actor_rep_id": entry.actor_rep_id,
            "approver_rep_id": entry.approver_rep_id,
            "action_type": entry.action_type,
            "target_type": entry.target_type,
            "target_id": entry.target_id,
            "prompt": _to_json(entry.prompt),
            "output": _to_json(entry.output),
            "metadata": _to_json(entry.metadata),
        },
    )
    return row.scalar_one()


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)
