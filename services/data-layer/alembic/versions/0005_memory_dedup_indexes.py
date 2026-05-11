"""Unique partial indexes on memory content hash, per tier.

Backs the MemoryStore's content-hash dedup with a real database-level
uniqueness constraint, so concurrent writes can rely on
``INSERT ... ON CONFLICT DO NOTHING`` instead of the racy
SELECT-then-INSERT pattern that the WO #7 review surfaced. The
``content_hash`` is the application's sha256(kind + content) and lives
in ``attributes->>'content_hash'``; we index that expression directly
rather than promote it to a column to keep the public schema stable.

The partial WHERE clause (``deleted_at IS NULL``) means a soft-deleted
row can be re-created — a fresh write with the same content after a
tombstone succeeds and inserts a new row.

Revision ID: 0005_memory_dedup_indexes
Revises: 0004_messaging_identities
Create Date: 2026-05-11

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_memory_dedup_indexes"
down_revision: str | Sequence[str] | None = "0004_messaging_identities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX rep_memories_dedup_uniq "
        "ON rep_memories (tenant_id, rep_id, (attributes->>'content_hash')) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX deal_memories_dedup_uniq "
        "ON deal_memories (tenant_id, deal_id, (attributes->>'content_hash')) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX account_memories_dedup_uniq "
        "ON account_memories (tenant_id, account_id, (attributes->>'content_hash')) "
        "WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX org_memories_dedup_uniq "
        "ON org_memories (tenant_id, (attributes->>'content_hash')) "
        "WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS org_memories_dedup_uniq")
    op.execute("DROP INDEX IF EXISTS account_memories_dedup_uniq")
    op.execute("DROP INDEX IF EXISTS deal_memories_dedup_uniq")
    op.execute("DROP INDEX IF EXISTS rep_memories_dedup_uniq")
