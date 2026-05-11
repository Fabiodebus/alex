"""Allow 'expired' in the task_state_status_chk check constraint.

The Approval Workflow (WO #12) distinguishes four terminal outcomes
per the blueprint: approved, edited, discarded, and expired. The first
three already mapped onto the existing 'completed' / 'cancelled'
statuses, but 'expired' (a task whose deadline elapsed without a rep
decision) wasn't representable. We extend the check constraint rather
than introduce a new column so the daily-brief surfacing query in a
future WO can stay a simple status filter.

Revision ID: 0006_task_state_expired
Revises: 0005_memory_dedup_indexes
Create Date: 2026-05-11

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_task_state_expired"
down_revision: str | Sequence[str] | None = "0005_memory_dedup_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_CHECK = (
    "status IN ('pending','in_progress','awaiting_approval',"
    "'completed','failed','cancelled')"
)

_NEW_CHECK = (
    "status IN ('pending','in_progress','awaiting_approval',"
    "'completed','failed','cancelled','expired')"
)


def upgrade() -> None:
    op.drop_constraint("task_state_status_chk", "task_state", type_="check")
    op.create_check_constraint(
        "task_state_status_chk",
        "task_state",
        _NEW_CHECK,
    )


def downgrade() -> None:
    # Move any 'expired' rows to 'cancelled' so the old check passes.
    # No data loss: the audit_log carries the original outcome.
    op.execute("UPDATE task_state SET status = 'cancelled' WHERE status = 'expired'")
    op.drop_constraint("task_state_status_chk", "task_state", type_="check")
    op.create_check_constraint(
        "task_state_status_chk",
        "task_state",
        _OLD_CHECK,
    )
