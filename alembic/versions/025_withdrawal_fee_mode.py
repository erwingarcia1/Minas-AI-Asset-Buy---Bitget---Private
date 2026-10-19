"""purchase_transactions.withdrawal_fee_mode: the fee mode a withdrawal was sent under

Revision ID: 025
Revises: 024
Create Date: 2026-09-21

Today the mode is derived from what the row recorded at send time
(expected_received == amount_executed -> exclusive; == amount_executed - fee
-> inclusive). Storing it explicitly makes the derivation unnecessary and
survives a future change to how expected_received is computed. The executor
would write bitget_market.fee_mode() into the column at send time. Existing
rows: NULL (readers fall back to the derivation).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '025'
down_revision: Union[str, None] = '024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchase_transactions', sa.Column('withdrawal_fee_mode', sa.String(length=10), nullable=True))
    op.create_check_constraint('ck_purchase_transactions_withdrawal_fee_mode', 'purchase_transactions',
                               "withdrawal_fee_mode IS NULL OR withdrawal_fee_mode IN ('exclusive', 'inclusive')")


def downgrade() -> None:
    op.drop_constraint('ck_purchase_transactions_withdrawal_fee_mode', 'purchase_transactions', type_='check')
    op.drop_column('purchase_transactions', 'withdrawal_fee_mode')
