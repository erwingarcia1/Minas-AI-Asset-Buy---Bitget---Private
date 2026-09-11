"""blockchain_network holds the exchange's chain name instead of mainnet/testnet

Revision ID: 018
Revises: 017
Create Date: 2026-09-11

Withdrawals go to a specific chain (Bitget names: BTC, ERC20, TRC20, SOL, ...),
not to "mainnet". purchases.blockchain_network and wallets.blockchain_network
now store that chain string. Existing rows are mapped by coin / address format:
BTC -> BTC, ETH/USDT/USDC -> ERC20, SOL -> SOL; wallets by address family
(bc1/1/3 -> BTC, 0x -> ERC20, else SOL). Legacy "testnet" rows map the same
way (exchanges withdraw to mainnet only); active withdraw-immediately
purchases that were on testnet are paused with an explanatory last_error so
the owner re-checks the wallet before anything is sent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '018'
down_revision: Union[str, None] = '017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('purchases', 'blockchain_network', existing_type=sa.VARCHAR(length=20), type_=sa.String(length=40))
    op.alter_column('wallets', 'blockchain_network', existing_type=sa.VARCHAR(length=20), type_=sa.String(length=40))
    # testnet autobuy purchases: pause before remapping so nothing is sent to a re-mapped chain unchecked
    op.execute("UPDATE purchases SET status = 'paused', next_execution = NULL, "
               "last_error = 'network changed from testnet to the exchange chain; re-check the wallet address and resume' "
               "WHERE lower(blockchain_network) = 'testnet' AND purchase_type = 'autobuy' AND status = 'active'")
    op.execute("UPDATE purchases SET blockchain_network = CASE upper(crypto_symbol) "
               "WHEN 'BTC' THEN 'BTC' WHEN 'SOL' THEN 'SOL' ELSE 'ERC20' END "
               "WHERE lower(blockchain_network) IN ('mainnet', 'testnet')")
    op.execute("UPDATE wallets SET blockchain_network = CASE "
               "WHEN address LIKE '0x%' THEN 'ERC20' "
               "WHEN address LIKE 'bc1%' OR address LIKE '1%' OR address LIKE '3%' THEN 'BTC' "
               "ELSE 'SOL' END "
               "WHERE lower(blockchain_network) IN ('mainnet', 'testnet')")


def downgrade() -> None:
    op.execute("UPDATE purchases SET blockchain_network = 'mainnet' WHERE lower(blockchain_network) NOT IN ('mainnet', 'testnet')")
    op.execute("UPDATE wallets SET blockchain_network = 'mainnet' WHERE lower(blockchain_network) NOT IN ('mainnet', 'testnet')")
    op.alter_column('wallets', 'blockchain_network', existing_type=sa.String(length=40), type_=sa.VARCHAR(length=20))
    op.alter_column('purchases', 'blockchain_network', existing_type=sa.String(length=40), type_=sa.VARCHAR(length=20))
