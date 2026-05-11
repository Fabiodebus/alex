"""onboarding_state table.

Backs the Onboarding capability (WO #15 + #16). One row per rep per
tenant — unique constraint enforces a single live onboarding sequence
per rep. The activation milestone + 24h proactive-output timer are
queried via the partial indexes below.

Revision ID: 0008_onboarding_state
Revises: 0007_delivery_statuses
Create Date: 2026-05-11

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_onboarding_state"
down_revision: str | Sequence[str] | None = "0007_delivery_statuses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_state",
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
        sa.Column(
            "current_step",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'welcome'"),
        ),
        sa.Column(
            "completed_steps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "connector_status",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Per-connector progress: {close: {status, token_ref, attempted_at}}",
        ),
        sa.Column(
            "pending_oauth",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="In-flight OAuth states keyed by `state` parameter -> {connector, started_at}",
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ingestion_complete_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_proactive_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activation_milestone_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
        sa.UniqueConstraint(
            "tenant_id", "rep_id", name="onboarding_state_tenant_rep_uniq"
        ),
        sa.CheckConstraint(
            "current_step IN ("
            "'welcome','connect_close','connect_google','connect_krisp',"
            "'ingesting','awaiting_first_output','completed','failed'"
            ")",
            name="onboarding_state_current_step_chk",
        ),
    )
    op.create_index(
        "onboarding_state_tenant_id_idx", "onboarding_state", ["tenant_id"]
    )
    op.create_index(
        "onboarding_state_pending_proactive_idx",
        "onboarding_state",
        ["ingestion_complete_at"],
        postgresql_where=sa.text(
            "first_proactive_at IS NULL AND ingestion_complete_at IS NOT NULL"
        ),
    )
    op.create_index(
        "onboarding_state_pending_activation_idx",
        "onboarding_state",
        ["first_proactive_at"],
        postgresql_where=sa.text(
            "activation_milestone_at IS NULL AND first_proactive_at IS NOT NULL"
        ),
    )

    op.execute("ALTER TABLE onboarding_state ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY onboarding_state_tenant_isolation ON onboarding_state "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS onboarding_state_tenant_isolation ON onboarding_state")
    op.drop_table("onboarding_state")
