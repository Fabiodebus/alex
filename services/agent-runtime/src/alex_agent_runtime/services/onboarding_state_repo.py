"""CRUD over the ``onboarding_state`` table (migration 0008).

The ``OnboardingConversationFlow`` and ``OAuthOrchestrator`` services
push state mutations through here so the table is the single source
of truth for "where is this rep in onboarding."
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from ..db import admin_session, transactional_session
from ..schemas import (
    ConnectorConnectionStatus,
    ConnectorStatus,
    OnboardingConnector,
    OnboardingState,
    OnboardingStep,
)
from ..tenant_context import tenant_scope

log = structlog.get_logger(__name__)


class OnboardingStateRepo:
    """Stateless CRUD wrapper."""

    async def get_or_create(self, *, tenant_id: UUID, rep_id: UUID) -> OnboardingState:
        existing = await self.get(tenant_id=tenant_id, rep_id=rep_id)
        if existing is not None:
            return existing
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        INSERT INTO onboarding_state (
                            tenant_id, rep_id, current_step,
                            completed_steps, connector_status, started_at
                        ) VALUES (
                            current_setting('app.tenant_id')::uuid,
                            :rep_id, 'welcome',
                            CAST('[]' AS jsonb),
                            CAST(:connector_status AS jsonb),
                            now()
                        )
                        ON CONFLICT (tenant_id, rep_id) DO UPDATE
                          SET updated_at = now()
                        RETURNING *
                        """
                    ),
                    {
                        "rep_id": rep_id,
                        "connector_status": json.dumps(
                            {c.value: {"status": "not_started"} for c in OnboardingConnector}
                        ),
                    },
                )
                return _row_to_state(row.mappings().one())

    async def get(self, *, tenant_id: UUID, rep_id: UUID) -> OnboardingState | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        SELECT * FROM onboarding_state
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        """
                    ),
                    {"rep_id": rep_id},
                )
                record = row.mappings().one_or_none()
        return _row_to_state(record) if record is not None else None

    async def advance_step(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        new_step: OnboardingStep,
        complete_previous: OnboardingStep | None = None,
    ) -> OnboardingState:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                if complete_previous is not None:
                    await session.execute(
                        text(
                            """
                            UPDATE onboarding_state
                               SET completed_steps =
                                       CASE
                                         WHEN completed_steps @> :step_jsonb
                                         THEN completed_steps
                                         ELSE completed_steps || :step_jsonb
                                       END
                             WHERE tenant_id = current_setting('app.tenant_id')::uuid
                               AND rep_id = :rep_id
                            """
                        ),
                        {
                            "step_jsonb": json.dumps([complete_previous.value]),
                            "rep_id": rep_id,
                        },
                    )
                row = await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET current_step = :step,
                               updated_at = now(),
                               completed_at = CASE
                                   WHEN :step = 'completed' AND completed_at IS NULL
                                   THEN now() ELSE completed_at
                               END
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        RETURNING *
                        """
                    ),
                    {"step": new_step.value, "rep_id": rep_id},
                )
                return _row_to_state(row.mappings().one())

    async def update_connector(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
        status: ConnectorConnectionStatus,
        token_ref: str | None = None,
        failure_reason: str | None = None,
    ) -> OnboardingState:
        slice_payload: dict[str, Any] = {
            "status": status.value,
            "attempted_at": _now_iso(),
        }
        if status is ConnectorConnectionStatus.CONNECTED:
            slice_payload["connected_at"] = _now_iso()
            slice_payload["token_ref"] = token_ref
        elif status is ConnectorConnectionStatus.FAILED:
            slice_payload["failure_reason"] = failure_reason

        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET connector_status = jsonb_set(
                                 COALESCE(connector_status, '{}'::jsonb),
                                 ARRAY[:connector_key],
                                 CAST(:slice_payload AS jsonb),
                                 true
                               ),
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        RETURNING *
                        """
                    ),
                    {
                        "connector_key": connector.value,
                        "slice_payload": json.dumps(slice_payload),
                        "rep_id": rep_id,
                    },
                )
                return _row_to_state(row.mappings().one())

    async def record_pending_oauth(
        self,
        *,
        tenant_id: UUID,
        rep_id: UUID,
        connector: OnboardingConnector,
        state: str,
    ) -> None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET pending_oauth = jsonb_set(
                                 COALESCE(pending_oauth, '{}'::jsonb),
                                 ARRAY[:state_key],
                                 CAST(:slice_payload AS jsonb),
                                 true
                               ),
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        """
                    ),
                    {
                        "state_key": state,
                        "slice_payload": json.dumps(
                            {"connector": connector.value, "started_at": _now_iso()}
                        ),
                        "rep_id": rep_id,
                    },
                )

    async def consume_pending_oauth(
        self, *, tenant_id: UUID, rep_id: UUID, state: str
    ) -> OnboardingConnector | None:
        """Pull the connector associated with ``state`` and clear it.

        Returns ``None`` if the state isn't recognised (e.g. already
        consumed, or never recorded — both are dropped silently).
        """
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        SELECT pending_oauth FROM onboarding_state
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        """
                    ),
                    {"rep_id": rep_id},
                )
                record = row.mappings().one_or_none()
                if record is None:
                    return None
                pending = record["pending_oauth"] or {}
                slice_ = pending.get(state)
                if not isinstance(slice_, dict):
                    return None
                connector_raw = slice_.get("connector")
                if not isinstance(connector_raw, str):
                    return None
                # Clear the slice in the same transaction.
                await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET pending_oauth = pending_oauth - :state_key,
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        """
                    ),
                    {"state_key": state, "rep_id": rep_id},
                )
        try:
            return OnboardingConnector(connector_raw)
        except ValueError:
            return None

    async def mark_ingestion_complete(
        self, *, tenant_id: UUID, rep_id: UUID
    ) -> OnboardingState | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET ingestion_complete_at = COALESCE(ingestion_complete_at, now()),
                               current_step = CASE
                                   WHEN current_step IN ('ingesting','connect_krisp','connect_google','connect_close','welcome')
                                   THEN 'awaiting_first_output' ELSE current_step
                               END,
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        RETURNING *
                        """
                    ),
                    {"rep_id": rep_id},
                )
                record = row.mappings().one_or_none()
        return _row_to_state(record) if record is not None else None

    async def mark_first_proactive(
        self, *, tenant_id: UUID, rep_id: UUID
    ) -> OnboardingState | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET first_proactive_at = COALESCE(first_proactive_at, now()),
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                        RETURNING *
                        """
                    ),
                    {"rep_id": rep_id},
                )
                record = row.mappings().one_or_none()
        return _row_to_state(record) if record is not None else None

    async def mark_activation_milestone(
        self, *, tenant_id: UUID, rep_id: UUID, task_id: UUID
    ) -> OnboardingState | None:
        with tenant_scope(tenant_id):
            async with transactional_session() as session:
                row = await session.execute(
                    text(
                        """
                        UPDATE onboarding_state
                           SET activation_milestone_at = COALESCE(activation_milestone_at, now()),
                               current_step = 'completed',
                               completed_at = COALESCE(completed_at, now()),
                               metadata = jsonb_set(
                                   COALESCE(metadata, '{}'::jsonb),
                                   ARRAY['activation_task_id'],
                                   CAST(:task_id AS jsonb)
                               ),
                               updated_at = now()
                         WHERE tenant_id = current_setting('app.tenant_id')::uuid
                           AND rep_id = :rep_id
                           AND activation_milestone_at IS NULL
                        RETURNING *
                        """
                    ),
                    {"rep_id": rep_id, "task_id": json.dumps(str(task_id))},
                )
                record = row.mappings().one_or_none()
        return _row_to_state(record) if record is not None else None

    # ------------------------------------------------------------------
    # Cross-tenant helpers used by the activation scan.
    # ------------------------------------------------------------------
    async def list_awaiting_first_proactive(
        self, *, older_than: datetime
    ) -> list[OnboardingState]:
        async with admin_session() as session:
            row = await session.execute(
                text(
                    """
                    SELECT *
                      FROM onboarding_state
                     WHERE first_proactive_at IS NULL
                       AND ingestion_complete_at IS NOT NULL
                       AND ingestion_complete_at < :older_than
                    """
                ),
                {"older_than": older_than},
            )
            return [_row_to_state(r) for r in row.mappings().all()]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _row_to_state(record: Any) -> OnboardingState:
    connector_status_raw = record["connector_status"] or {}
    parsed_status: dict[OnboardingConnector, ConnectorStatus] = {}
    for raw_key, raw_value in connector_status_raw.items():
        try:
            connector = OnboardingConnector(raw_key)
        except ValueError:
            continue
        if not isinstance(raw_value, dict):
            continue
        parsed_status[connector] = ConnectorStatus(**_coerce_connector_slice(raw_value))

    completed_raw = record["completed_steps"] or []
    completed_steps: list[OnboardingStep] = []
    for v in completed_raw if isinstance(completed_raw, list) else []:
        try:
            completed_steps.append(OnboardingStep(v))
        except (ValueError, TypeError):
            continue

    return OnboardingState(
        id=record["id"],
        tenant_id=record["tenant_id"],
        rep_id=record["rep_id"],
        current_step=OnboardingStep(record["current_step"]),
        completed_steps=completed_steps,
        connector_status=parsed_status,
        started_at=record["started_at"],
        ingestion_complete_at=record["ingestion_complete_at"],
        first_proactive_at=record["first_proactive_at"],
        activation_milestone_at=record["activation_milestone_at"],
        completed_at=record["completed_at"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _coerce_connector_slice(slice_: dict[str, Any]) -> dict[str, Any]:
    """Translate raw JSONB into ConnectorStatus kwargs.

    ISO-string timestamps from JSON become datetime objects; missing
    status defaults to NOT_STARTED."""
    out: dict[str, Any] = {}
    raw_status = slice_.get("status")
    try:
        out["status"] = ConnectorConnectionStatus(raw_status) if raw_status else ConnectorConnectionStatus.NOT_STARTED
    except ValueError:
        out["status"] = ConnectorConnectionStatus.NOT_STARTED
    for key in ("attempted_at", "connected_at"):
        v = slice_.get(key)
        if isinstance(v, str):
            try:
                out[key] = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                pass
    if isinstance(slice_.get("token_ref"), str):
        out["token_ref"] = slice_["token_ref"]
    if isinstance(slice_.get("failure_reason"), str):
        out["failure_reason"] = slice_["failure_reason"]
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
