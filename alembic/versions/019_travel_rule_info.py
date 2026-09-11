"""purchases.travel_rule_info: optional recipient details for exchange withdrawals

Revision ID: 019
Revises: 018
Create Date: 2026-09-11

Some venues (Bitget among them) reject an API withdrawal until travel-rule /
beneficiary details are supplied. They are stored as an optional JSON blob on
the purchase and sent with the withdrawal when present; a rejection for
missing data parks the purchase as needs_info and the user is emailed. There
is no automatic retry.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '019'
down_revision: Union[str, None] = '018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchases', sa.Column('travel_rule_info', sa.String(length=1000), nullable=True))


def downgrade() -> None:
    op.drop_column('purchases', 'travel_rule_info')
