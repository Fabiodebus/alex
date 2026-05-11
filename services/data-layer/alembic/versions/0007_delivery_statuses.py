"""delivery_statuses table.

Backing store for :class:`DeliveryTracker` (WO #13). One row per
:class:`AgentOutput` dispatch attempt: records which channel was
chosen, how many attempts have been made, the last surface response,
and whether the delivery was escalated for rep attention. Daily-brief
assembly queries this table to skip already-delivered items.

Revision ID: 0007_delivery_statuses
Revises: 0006_task_state_expired
Create Date: 2026-05-11

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_delivery_statuses"
down_revision: str | Sequence[str] | None = "0006_task_state_expired"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_statuses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rep_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # task_id is the optional ApprovalGate.PendingTask id this
        # output corresponds to. No FK constraint: informational
        # outputs don't have a task at all, and we don't want to fail
        # the INSERT just because a task was hard-deleted or never
        # existed (e.g. surface notifications that aren't approval-
        # driven). Queries that need to join can still do so on
        # equality.
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("output_id", sa.Text(), nullable=False),
        sa.Column("output_type", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retry_after", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','delivered','failed','escalated')",
            name="delivery_statuses_status_chk",
        ),
        sa.CheckConstraint(
            "channel IN ('slack','teams','crm_native')",
            name="delivery_statuses_channel_chk",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "output_id",
            name="delivery_statuses_tenant_output_uniq",
        ),
    )
    op.create_index(
        "delivery_statuses_tenant_rep_created_idx",
        "delivery_statuses",
        ["tenant_id", "rep_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "delivery_statuses_pending_idx",
        "delivery_statuses",
        ["status", "retry_after"],
        postgresql_where=sa.text("status IN ('pending','failed')"),
    )

    op.execute("ALTER TABLE delivery_statuses ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY delivery_statuses_tenant_isolation ON delivery_statuses "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS delivery_statuses_tenant_isolation ON delivery_statuses")
    op.drop_table("delivery_statuses")
