"""buy idempotency (client order id stored before sending) and separate withdrawal rows

Revision ID: 017
Revises: 016
Create Date: 2026-09-11

purchase_transactions gains:
- client_oid: our unique order id, written BEFORE the order is sent, so an
  attempt whose outcome is unknown (timeout after placing) can be looked up on
  the exchange and never re-bought.
- tx_type 'buy': the buy is its own row; a withdrawal is a separate row whose
  parent_transaction_id points at the buy.
- raw_status: the exchange's own status string (filled, wallet_processing, ...).
- withdrawal_chain: the exchange chain name used for a withdrawal.
Statuses gain needs_review (outcome unknown; human check) and needs_info
(withdrawal rejected for missing travel-rule/identity data); both are plain
strings on the existing status column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '017'
down_revision: Union[str, None] = '016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE transaction_type ADD VALUE IF NOT EXISTS 'buy'")
    op.add_column('purchase_transactions', sa.Column('client_oid', sa.String(length=64), nullable=True))
    op.add_column('purchase_transactions', sa.Column('parent_transaction_id', postgresql.UUID(as_uuid=True),
                                                     sa.ForeignKey('purchase_transactions.id', ondelete='SET NULL'),
                                                     nullable=True))
    op.add_column('purchase_transactions', sa.Column('raw_status', sa.String(length=40), nullable=True))
    op.add_column('purchase_transactions', sa.Column('withdrawal_chain', sa.String(length=40), nullable=True))
    op.create_index('uq_purchase_transactions_client_oid', 'purchase_transactions', ['client_oid'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_purchase_transactions_client_oid', table_name='purchase_transactions')
    for col in ('withdrawal_chain', 'raw_status', 'parent_transaction_id', 'client_oid'):
        op.drop_column('purchase_transactions', col)
    # the enum value 'buy' cannot be removed from a Postgres enum; rows using it must be converted first
