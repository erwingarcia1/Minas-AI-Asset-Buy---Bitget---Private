"""drop the legacy recurring-purchase tables

Revision ID: 014
Revises: 013
Create Date: 2026-09-11

Retires the legacy schedule system for good: ``transactions`` (the old
execution history, written only by the removed purchase_service executor)
and ``recurring_purchases`` (the schedules, including the advanced-purchase
columns from migration 006 and the ``exchange`` column from 009) are dropped.
``purchase_transactions`` (migration 013) is the only execution history.

Nothing references either table any more: the FK ``transactions.recurring_purchase_id``
goes with the ``transactions`` table, and no other table has a foreign key
to ``recurring_purchases`` or ``transactions``.

Take a ``pg_dump`` before running this on a database that holds real rows;
the downgrade recreates both tables empty (schema as of migration 013).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '014'
down_revision: Union[str, None] = '013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # transactions first: it carries the FK to recurring_purchases.
    op.drop_index('ix_transactions_user_id_status', table_name='transactions')
    op.drop_index('ix_transactions_executed_at', table_name='transactions')
    op.drop_index('ix_transactions_binance_order_id', table_name='transactions')
    op.drop_index('ix_transactions_user_id_created_at', table_name='transactions')
    op.drop_table('transactions')
    op.drop_index('ix_recurring_purchases_enabled_next', table_name='recurring_purchases')
    op.drop_table('recurring_purchases')


def downgrade() -> None:
    # Schema as it stood at migration 013 (empty tables; data is not restored).
    op.create_table(
        'recurring_purchases',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('crypto', sa.String(length=10), nullable=False),
        sa.Column('exchange', sa.String(length=20), nullable=False, server_default='binance'),
        sa.Column('amount_usdt', sa.Float(), nullable=False),
        sa.Column('frequency', sa.String(length=50), nullable=False),
        sa.Column('next_purchase_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('withdrawal_address', sa.String(length=255), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=True),
        sa.Column('purchase_type', sa.String(length=20), nullable=False, server_default='recurring'),
        sa.Column('limit_price', sa.Float(), nullable=True),
        sa.Column('min_price', sa.Float(), nullable=True),
        sa.Column('max_price', sa.Float(), nullable=True),
        sa.Column('volatility_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('target_allocation', sa.Float(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_recurring_purchases_enabled_next', 'recurring_purchases', ['enabled', 'next_purchase_date'])
    op.create_table(
        'transactions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('recurring_purchase_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('crypto', sa.String(length=10), nullable=False),
        sa.Column('amount_crypto', sa.Float(), nullable=False),
        sa.Column('cost_usdt', sa.Float(), nullable=False),
        sa.Column('binance_order_id', sa.String(length=255), nullable=True),
        sa.Column('exchange_rate', sa.Float(), nullable=True),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['recurring_purchase_id'], ['recurring_purchases.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_transactions_user_id_created_at', 'transactions', ['user_id', 'created_at'])
    op.create_index('ix_transactions_binance_order_id', 'transactions', ['binance_order_id'])
    op.create_index('ix_transactions_executed_at', 'transactions', ['executed_at'])
    op.create_index('ix_transactions_user_id_status', 'transactions', ['user_id', 'status'])
