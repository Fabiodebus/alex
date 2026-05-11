"""oauth_connections table.

Tracks per-rep OAuth state for each integration source. Pipedream owns the
encrypted token material in its vault; this table is the Agent Runtime's
view of *which* connections are active for *which* reps, so feature
workflows can short-circuit when the rep hasn't connected the required
integration yet.

Revision ID: 0003_oauth_connections
Revises: 0002_processed_events
Create Date: 2026-05-11

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_oauth_connections"
down_revision: str | Sequence[str] | None = "0002_processed_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_connections",
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
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'connected'"),
        ),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("vault_ref", sa.Text(), nullable=True),
        sa.Column(
            "connected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('connected','disconnected','expired','revoked','error')",
            name="oauth_connections_status_chk",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "rep_id",
            "source",
            name="oauth_connections_tenant_rep_source_uniq",
        ),
    )
    op.create_index(
        "oauth_connections_tenant_id_idx",
        "oauth_connections",
        ["tenant_id"],
    )
    op.create_index(
        "oauth_connections_rep_source_idx",
        "oauth_connections",
        ["rep_id", "source"],
    )

    op.execute("ALTER TABLE oauth_connections ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY oauth_connections_tenant_isolation ON oauth_connections "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )
    # `last_seen_at` is set explicitly by the connection_repo on every
    # status touch; no DB-side trigger needed (and the shared
    # set_updated_at() trigger targets an `updated_at` column this table
    # doesn't have).


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS oauth_connections_tenant_isolation ON oauth_connections")
    op.drop_table("oauth_connections")
