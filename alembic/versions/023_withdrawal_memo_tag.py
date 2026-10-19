"""memo / destination tag and destination type for chains that require a tag (XRP, Stellar)

Revision ID: 023
Revises: 022
Create Date: 2026-09-16

Bitget's coins endpoint reports needTag=true for the XRP and StellarLumens
chains. A withdrawal to an exchange or custodial address on those chains
without the right tag lands in a pooled account and cannot be attributed to
the customer.

Two facts are stored, because "no tag needed" and "nobody answered" must be
distinguishable to the executor:

  destination_type   self_custody | exchange   who the address belongs to, as the
                                               customer declared it (NULL = never asked;
                                               every existing row, all on tag-free chains)
  memo               the tag/memo itself; required when destination_type is exchange on a
                     chain that needs a tag; must be empty for self_custody

Columns:
  wallets.destination_type, wallets.memo                    the address-book entry
  purchases.wallet_destination_type, purchases.wallet_memo  the purchase's withdrawal destination
  purchase_transactions.withdrawal_memo                     what was actually sent with each withdrawal row

All nullable; existing rows keep NULL. Memo max 64 chars: XRP destination tags
are unsigned 32-bit integers (<= 10 digits), Stellar text memos <= 28 bytes,
Stellar id memos unsigned 64-bit (<= 20 digits).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '023'
down_revision: Union[str, None] = '022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('wallets', sa.Column('destination_type', sa.String(length=20), nullable=True))
    op.add_column('wallets', sa.Column('memo', sa.String(length=64), nullable=True))
    op.add_column('purchases', sa.Column('wallet_destination_type', sa.String(length=20), nullable=True))
    op.add_column('purchases', sa.Column('wallet_memo', sa.String(length=64), nullable=True))
    op.add_column('purchase_transactions', sa.Column('withdrawal_memo', sa.String(length=64), nullable=True))
    op.create_check_constraint('ck_wallets_destination_type', 'wallets',
                               "destination_type IS NULL OR destination_type IN ('self_custody', 'exchange')")
    op.create_check_constraint('ck_purchases_wallet_destination_type', 'purchases',
                               "wallet_destination_type IS NULL OR wallet_destination_type IN ('self_custody', 'exchange')")


def downgrade() -> None:
    op.drop_constraint('ck_purchases_wallet_destination_type', 'purchases', type_='check')
    op.drop_constraint('ck_wallets_destination_type', 'wallets', type_='check')
    op.drop_column('purchase_transactions', 'withdrawal_memo')
    op.drop_column('purchases', 'wallet_memo')
    op.drop_column('purchases', 'wallet_destination_type')
    op.drop_column('wallets', 'memo')
    op.drop_column('wallets', 'destination_type')
