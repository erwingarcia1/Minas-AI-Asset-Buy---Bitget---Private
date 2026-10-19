"""purchase_transactions.expected_received; purchases.blockchain_network optional

Revision ID: 021
Revises: 020
Create Date: 2026-09-13

A withdrawal row now stores both the amount handed to the exchange
(amount_executed) and what the wallet should receive after the chain fee
(expected_received), which depends on BITGET_WITHDRAW_FEE_MODE. Accumulate
purchases hold coins on the exchange, so they no longer need a chain; the
column becomes nullable (existing values are kept).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '021'
down_revision: Union[str, None] = '020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchase_transactions', sa.Column('expected_received', sa.Float(), nullable=True))
    op.alter_column('purchases', 'blockchain_network', existing_type=sa.String(length=40), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE purchases SET blockchain_network = 'BTC' WHERE blockchain_network IS NULL")
    op.alter_column('purchases', 'blockchain_network', existing_type=sa.String(length=40), nullable=False)
    op.drop_column('purchase_transactions', 'expected_received')
