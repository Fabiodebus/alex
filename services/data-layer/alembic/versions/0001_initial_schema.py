"""Initial schema for Alex's data layer.

Creates the tenant identity tables, the four memory tiers (rep / deal /
account / org) with their pgvector embedding tables, the operational
``task_state`` and ``tenant_config`` tables, and the append-only
``audit_log``. Row-level security is enabled on every tenant-scoped
table and a trigger blocks UPDATE / DELETE on ``audit_log`` unless an
admin GDPR purge GUC is set.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-08

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Default embedding dimension. OpenAI text-embedding-3-small produces 1536-d
# vectors; Voyage / Cohere multilingual produce 1024-d. Phase 2's
# EmbeddingIndexer must keep its model in sync, or a follow-up migration must
# alter the affected `content_vector` columns and rebuild the HNSW indexes.
EMBEDDING_DIM = 1536

# Tenant-scoped tables that get a row-level security policy keyed off the
# `app.tenant_id` GUC. Order does not matter for RLS, only for table create.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "tenant_config",
    "reps",
    "accounts",
    "deals",
    "rep_memories",
    "deal_memories",
    "account_memories",
    "org_memories",
    "rep_memory_embeddings",
    "deal_memory_embeddings",
    "account_memory_embeddings",
    "org_memory_embeddings",
    "task_state",
    "audit_log",
)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _tenant_fk(*, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete=ondelete),
        nullable=False,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _deleted_at() -> sa.Column:
    # Soft delete marker. GDPR right-to-deletion is enforced by hard DELETEs
    # cascading through foreign keys; this column is for normal lifecycle
    # deactivations (e.g., user offboarding pending review).
    return sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # gen_random_uuid() is a core function in PostgreSQL 13+. pgcrypto is
    # installed as a fallback for clusters where it lives in pgcrypto only.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # Identity tables
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default=sa.text("'eu-central-1'")),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
        _updated_at(),
        _deleted_at(),
    )
    op.create_index("tenants_name_idx", "tenants", ["name"])

    op.create_table(
        "tenant_config",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("tenant_id", "key", name="tenant_config_tenant_key_uniq"),
    )
    op.create_index("tenant_config_tenant_id_idx", "tenant_config", ["tenant_id"])

    op.create_table(
        "reps",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False, server_default=sa.text("'en-US'")),
        sa.Column(
            "preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
        _updated_at(),
        _deleted_at(),
    )
    op.create_index("reps_tenant_id_idx", "reps", ["tenant_id"])
    # Case-insensitive uniqueness per tenant. Using a functional index keeps
    # the schema citext-free.
    op.execute(
        "CREATE UNIQUE INDEX reps_tenant_email_uniq "
        "ON reps (tenant_id, lower(email)) WHERE deleted_at IS NULL"
    )

    op.create_table(
        "accounts",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("external_crm", sa.Text(), nullable=True),
        sa.Column("external_crm_id", sa.Text(), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
        _updated_at(),
        _deleted_at(),
    )
    op.create_index("accounts_tenant_id_idx", "accounts", ["tenant_id"])
    op.execute(
        "CREATE UNIQUE INDEX accounts_tenant_external_uniq "
        "ON accounts (tenant_id, external_crm, external_crm_id) "
        "WHERE external_crm IS NOT NULL AND external_crm_id IS NOT NULL"
    )

    op.create_table(
        "deals",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_rep_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("external_crm", sa.Text(), nullable=True),
        sa.Column("external_crm_id", sa.Text(), nullable=True),
        sa.Column("expected_close_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
        _updated_at(),
        _deleted_at(),
    )
    op.create_index("deals_tenant_id_idx", "deals", ["tenant_id"])
    op.create_index("deals_account_id_idx", "deals", ["account_id"])
    op.create_index("deals_owner_rep_id_idx", "deals", ["owner_rep_id"])
    op.execute(
        "CREATE UNIQUE INDEX deals_tenant_external_uniq "
        "ON deals (tenant_id, external_crm, external_crm_id) "
        "WHERE external_crm IS NOT NULL AND external_crm_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # Memory tier tables
    # ------------------------------------------------------------------
    _create_memory_table(
        table="rep_memories",
        owner_column="rep_id",
        owner_ref="reps.id",
    )
    _create_memory_table(
        table="deal_memories",
        owner_column="deal_id",
        owner_ref="deals.id",
    )
    _create_memory_table(
        table="account_memories",
        owner_column="account_id",
        owner_ref="accounts.id",
    )
    # org_memories is scoped only to a tenant — there is no narrower owner.
    _create_memory_table(
        table="org_memories",
        owner_column=None,
        owner_ref=None,
    )

    # ------------------------------------------------------------------
    # Embedding tables
    # ------------------------------------------------------------------
    _create_embedding_table("rep_memory_embeddings", "rep_memories")
    _create_embedding_table("deal_memory_embeddings", "deal_memories")
    _create_embedding_table("account_memory_embeddings", "account_memories")
    _create_embedding_table("org_memory_embeddings", "org_memories")

    # ------------------------------------------------------------------
    # Operational tables
    # ------------------------------------------------------------------
    op.create_table(
        "task_state",
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "parent_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_state.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assignee_rep_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','awaiting_approval','completed','failed','cancelled')",
            name="task_state_status_chk",
        ),
    )
    op.create_index("task_state_tenant_id_idx", "task_state", ["tenant_id"])
    op.create_index(
        "task_state_tenant_status_scheduled_idx",
        "task_state",
        ["tenant_id", "status", "scheduled_for"],
    )
    op.create_index("task_state_assignee_rep_id_idx", "task_state", ["assignee_rep_id"])

    op.create_table(
        "audit_log",
        _uuid_pk(),
        _tenant_fk(),  # ON DELETE CASCADE — gated by the append-only trigger.
        sa.Column(
            "actor_rep_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approver_rep_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "prompt",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
    )
    op.create_index(
        "audit_log_tenant_created_at_idx",
        "audit_log",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "audit_log_target_idx",
        "audit_log",
        ["target_type", "target_id"],
        postgresql_where=sa.text("target_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # Append-only enforcement on audit_log
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_block_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'audit_log is append-only: UPDATE not permitted';
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.allow_audit_purge', true) = 'true' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION
                    'audit_log is append-only: DELETE requires app.allow_audit_purge=true (admin GDPR purge path)';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update "
        "BEFORE UPDATE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_delete "
        "BEFORE DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation()"
    )

    # ------------------------------------------------------------------
    # updated_at maintenance
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$;
        """
    )
    for table in (
        "tenants",
        "tenant_config",
        "reps",
        "accounts",
        "deals",
        "rep_memories",
        "deal_memories",
        "account_memories",
        "org_memories",
        "task_state",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at "
            f"BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    # ------------------------------------------------------------------
    # Row-level security
    #
    # Policies use `current_setting('app.tenant_id', true)` so a missing
    # GUC returns NULL instead of erroring; `tenant_id = NULL` is false,
    # which means rows are inaccessible by default. Application connections
    # MUST `SET LOCAL app.tenant_id = '<uuid>'` at the top of every
    # transaction. RLS is enabled but not FORCED so the schema-owning role
    # (used by Alembic) bypasses the policies for migrations.
    # ------------------------------------------------------------------
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
            f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
        )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------
def downgrade() -> None:
    # Drop policies first so the tables can be dropped cleanly.
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    for table in (
        "tenants",
        "tenant_config",
        "reps",
        "accounts",
        "deals",
        "rep_memories",
        "deal_memories",
        "account_memories",
        "org_memories",
        "task_state",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON {table}")

    op.execute("DROP FUNCTION IF EXISTS audit_log_block_mutation()")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")

    # Tables in reverse FK order.
    op.drop_table("audit_log")
    op.drop_table("task_state")
    op.drop_table("org_memory_embeddings")
    op.drop_table("account_memory_embeddings")
    op.drop_table("deal_memory_embeddings")
    op.drop_table("rep_memory_embeddings")
    op.drop_table("org_memories")
    op.drop_table("account_memories")
    op.drop_table("deal_memories")
    op.drop_table("rep_memories")
    op.drop_table("deals")
    op.drop_table("accounts")
    op.drop_table("reps")
    op.drop_table("tenant_config")
    op.drop_table("tenants")

    # Leave the extensions installed — other databases on the cluster may
    # rely on them and re-creating them is cheap.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _create_memory_table(
    *,
    table: str,
    owner_column: str | None,
    owner_ref: str | None,
) -> None:
    """Create a memory tier table (rep / deal / account / org)."""
    columns: list[sa.Column] = [
        _uuid_pk(),
        _tenant_fk(),
    ]
    if owner_column is not None and owner_ref is not None:
        columns.append(
            sa.Column(
                owner_column,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(owner_ref, ondelete="CASCADE"),
                nullable=False,
            )
        )
    columns.extend(
        [
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column(
                "attributes",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("source_uri", sa.Text(), nullable=True),
            _created_at(),
            _updated_at(),
            _deleted_at(),
        ]
    )
    op.create_table(table, *columns)
    op.create_index(f"{table}_tenant_id_idx", table, ["tenant_id"])
    op.create_index(f"{table}_kind_idx", table, ["tenant_id", "kind"])
    if owner_column is not None:
        op.create_index(f"{table}_{owner_column}_idx", table, [owner_column])


def _create_embedding_table(table: str, source_table: str) -> None:
    """Create a `*_embeddings` table associated with a memory tier table."""
    op.create_table(
        table,
        _uuid_pk(),
        _tenant_fk(),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{source_table}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_vector", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        _created_at(),
    )
    op.create_index(f"{table}_tenant_id_idx", table, ["tenant_id"])
    op.create_index(f"{table}_source_id_idx", table, ["source_id"])
    op.execute(
        f"CREATE INDEX {table}_content_vector_hnsw_idx "
        f"ON {table} USING hnsw (content_vector vector_cosine_ops)"
    )
