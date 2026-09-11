"""fill details on purchase_transactions

Revision ID: 013
Revises: 012
Create Date: 2026-09-11

purchase_transactions becomes the single source of truth for history, the
dashboard, tax exports and notifications, so every attempt records what the
exchange reported: order id, fill price, quote spent, fee + currency, and for
withdrawals the withdrawal id, fee + currency and on-chain hash.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '013'
down_revision: Union[str, None] = '012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    sa.Column('order_id', sa.String(length=128), nullable=True),
    sa.Column('fill_price', sa.Float(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('fee_currency', sa.String(length=10), nullable=True),
    sa.Column('withdrawal_id', sa.String(length=128), nullable=True),
    sa.Column('withdrawal_fee', sa.Float(), nullable=True),
    sa.Column('withdrawal_fee_currency', sa.String(length=10), nullable=True),
    sa.Column('tx_hash', sa.String(length=128), nullable=True),
)


def upgrade() -> None:
    for col in COLUMNS:
        op.add_column('purchase_transactions', col)
    # Older rows: the exchange_tx_id was the order id (accumulation) or the
    # withdrawal id (withdrawal); the quote spent was the purchase amount.
    op.execute("UPDATE purchase_transactions SET order_id = exchange_tx_id WHERE tx_type = 'accumulation'")
    op.execute("UPDATE purchase_transactions SET withdrawal_id = exchange_tx_id WHERE tx_type = 'withdrawal'")
    op.execute("UPDATE purchase_transactions t SET cost_usd = p.amount_usd FROM purchases p "
               "WHERE p.id = t.purchase_id AND t.status = 'success' AND t.cost_usd IS NULL")
    op.create_index('ix_purchase_transactions_status_created', 'purchase_transactions', ['status', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_purchase_transactions_status_created', table_name='purchase_transactions')
    for col in reversed(COLUMNS):
        op.drop_column('purchase_transactions', col.name)
