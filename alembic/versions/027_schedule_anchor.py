"""purchases.schedule_anchor: the fixed point a recurring schedule is computed from

Revision ID: 027
Revises: 026
Create Date: 2026-10-06

Until now the next run was computed from the moment a buy executed, so the
scheduler's 5-minute tick offset accumulated: the weekly XRP buy moved from
00:12 to 00:16 UTC in one week. The anchor is set at creation and whenever the
frequency is edited, and never by execution, failure, pause or resume; the
next run is the first anchor + k × period strictly after now (monthly: the
anchor's day and clock, clamped to the month, computed from the anchor each
time so a short month never sticks). Backfill: active and paused purchases
anchor on their current next_execution, everything else on created_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '027'
down_revision: Union[str, None] = '026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchases', sa.Column('schedule_anchor', sa.DateTime(timezone=True), nullable=True))
    op.execute("""
        UPDATE purchases SET schedule_anchor = CASE
            WHEN status IN ('active', 'paused') AND next_execution IS NOT NULL THEN next_execution
            ELSE created_at END
        WHERE schedule_anchor IS NULL
    """)
    op.alter_column('purchases', 'schedule_anchor', nullable=False)


def downgrade() -> None:
    op.drop_column('purchases', 'schedule_anchor')
