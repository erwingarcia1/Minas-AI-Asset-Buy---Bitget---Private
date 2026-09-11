"""wallet_address optional for accumulate (dca) purchases

Revision ID: 016
Revises: 015
Create Date: 2026-09-11

A dca (accumulate on exchange) purchase never withdraws, so it no longer needs
a wallet on file. Denylisted placeholder/burn addresses that were entered on
dca purchases only to satisfy the old requirement are cleared. autobuy
purchases keep requiring a real address (enforced by the API).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '016'
down_revision: Union[str, None] = '015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLACEHOLDERS = (
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "1111111111111111111114oLvT2", "1BitcoinEaterAddressDontSendf59kuE",
    "1CounterpartyXXXXXXXXXXXXXXXUXCHtA", "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dEaD", "11111111111111111111111111111111",
)


def upgrade() -> None:
    op.alter_column('purchases', 'wallet_address', existing_type=sa.VARCHAR(length=128), nullable=True)
    placeholders = ", ".join(f"'{p}'" for p in PLACEHOLDERS)
    op.execute(f"UPDATE purchases SET wallet_address = NULL WHERE purchase_type = 'dca' "
               f"AND (wallet_address = '' OR lower(wallet_address) IN ({placeholders.lower()}))")


def downgrade() -> None:
    op.execute("UPDATE purchases SET wallet_address = '' WHERE wallet_address IS NULL")
    op.alter_column('purchases', 'wallet_address', existing_type=sa.VARCHAR(length=128), nullable=False)
