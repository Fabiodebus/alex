"""messaging_identities table.

Maps a rep's Alex identity to their identity on each messaging platform
(Slack today, Teams in WO #6). The Agent Runtime resolves the right
external_user_id + dm_channel_id when dispatching an AgentOutput to a
particular delivery channel, so the messaging surface itself stays
stateless.

Revision ID: 0004_messaging_identities
Revises: 0003_oauth_connections
Create Date: 2026-05-11

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_messaging_identities"
down_revision: str | Sequence[str] | None = "0003_oauth_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "messaging_identities",
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
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column("external_team_id", sa.Text(), nullable=True),
        sa.Column("dm_channel_id", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "platform IN ('slack','teams')",
            name="messaging_identities_platform_chk",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform",
            "external_user_id",
            name="messaging_identities_platform_user_uniq",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "rep_id",
            "platform",
            name="messaging_identities_rep_platform_uniq",
        ),
    )
    op.create_index("messaging_identities_tenant_id_idx", "messaging_identities", ["tenant_id"])
    op.create_index(
        "messaging_identities_rep_platform_idx",
        "messaging_identities",
        ["rep_id", "platform"],
    )

    op.execute("ALTER TABLE messaging_identities ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY messaging_identities_tenant_isolation ON messaging_identities "
        "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS messaging_identities_tenant_isolation ON messaging_identities")
    op.drop_table("messaging_identities")
