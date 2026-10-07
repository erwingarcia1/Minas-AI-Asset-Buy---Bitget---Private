"""purchases.low_balance_alerted_for / _at: pre-buy low-balance alert bookkeeping

Revision ID: 026
Revises: 025
Create Date: 2026-10-01

A read-only scheduled job warns a customer when the USDT in their exchange
sub-account looks too low for an upcoming scheduled buy. These two columns
record which run the last warning was for and when it was sent, so one run
gets at most one email and the dashboard banner clears once a later check
finds enough balance (or the run has passed). Both NULL when nothing is
outstanding. Existing rows: NULL. No backfill, no index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '026'
down_revision: Union[str, None] = '025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchases', sa.Column('low_balance_alerted_for', sa.DateTime(timezone=True), nullable=True))
    op.add_column('purchases', sa.Column('low_balance_alerted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('purchases', 'low_balance_alerted_at')
    op.drop_column('purchases', 'low_balance_alerted_for')
