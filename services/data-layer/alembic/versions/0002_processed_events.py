"""processed_events table for /events idempotency.

Adds a tenant-scoped table the Agent Runtime uses to deduplicate
``IntegrationEvent`` payloads received from the Pipedream Integration
Layer. Composite primary key ``(tenant_id, event_id)`` makes the second
INSERT of a duplicate event raise an integrity error the runtime can
swallow as an idempotent ack.

Revision ID: 0002_processed_events
Revises: 0001_initial_schema
Create Date: 2026-05-08

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_processed_events"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "event_id", name="processed_events_pkey"),
    )
    op.create_index("processed_events_received_at_idx", "processed_events", ["received_at"])
    op.create_index("processed_events_kind_idx", "processed_events", ["tenant_id", "kind"])

    op.execute("ALTER TABLE processed_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY processed_events_tenant_isolation ON processed_events "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS processed_events_tenant_isolation ON processed_events")
    op.drop_table("processed_events")
