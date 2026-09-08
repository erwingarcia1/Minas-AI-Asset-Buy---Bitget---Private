"""purchase_type enum on purchases, tx_type on purchase_transactions

Revision ID: 012
Revises: 011
Create Date: 2026-09-08

Replaces the boolean withdraw_immediately (011) with purchase_type
('autobuy' | 'dca'), backfilling dca from rows that had it false, and makes
wallet_address required again (both modes need one). Adds tx_type
('withdrawal' | 'accumulation') to purchase_transactions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '012'
down_revision: Union[str, None] = '011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

purchase_type = postgresql.ENUM('autobuy', 'dca', name='purchase_type', create_type=False)
transaction_type = postgresql.ENUM('withdrawal', 'accumulation', name='transaction_type', create_type=False)


def upgrade() -> None:
    purchase_type.create(op.get_bind(), checkfirst=True)
    transaction_type.create(op.get_bind(), checkfirst=True)
    op.add_column('purchases', sa.Column('purchase_type', purchase_type, nullable=False, server_default='autobuy'))
    op.execute("UPDATE purchases SET purchase_type = 'dca' WHERE withdraw_immediately = false")
    op.drop_column('purchases', 'withdraw_immediately')
    # dca rows created under 011 could lack a wallet; a wallet is now required in both modes
    op.execute("UPDATE purchases SET status = 'paused', last_error = 'wallet_address required: add one and resume' "
               "WHERE wallet_address IS NULL AND status = 'active'")
    op.execute("UPDATE purchases SET wallet_address = '' WHERE wallet_address IS NULL")
    op.alter_column('purchases', 'wallet_address', existing_type=sa.VARCHAR(length=128), nullable=False)
    op.add_column('purchase_transactions',
                  sa.Column('tx_type', transaction_type, nullable=False, server_default='withdrawal'))


def downgrade() -> None:
    op.drop_column('purchase_transactions', 'tx_type')
    op.add_column('purchases', sa.Column('withdraw_immediately', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.execute("UPDATE purchases SET withdraw_immediately = false WHERE purchase_type = 'dca'")
    op.alter_column('purchases', 'wallet_address', existing_type=sa.VARCHAR(length=128), nullable=True)
    op.drop_column('purchases', 'purchase_type')
    transaction_type.drop(op.get_bind(), checkfirst=True)
    purchase_type.drop(op.get_bind(), checkfirst=True)
