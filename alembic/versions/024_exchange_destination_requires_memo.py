"""CHECK: an exchange destination must carry a memo (wallets, purchases)

Revision ID: 024
Revises: 023
Create Date: 2026-09-17

Migration 023 added destination_type / memo. The API and the executor already
refuse an exchange destination without its tag; this constraint makes that
row physically unwritable, so the executor's send-time refusal becomes a
backstop instead of the only defence against a direct write or a future code
path.

  wallets:    destination_type <> 'exchange' OR memo IS NOT NULL
  purchases:  wallet_destination_type <> 'exchange' OR wallet_memo IS NOT NULL

NULL destination_type passes (NULL <> 'exchange' is NULL, and CHECK treats
NULL as not violated): "never answered" stays the executor's job, because the
database cannot know which chains need a tag. Existing rows all have a NULL
type (every chain in use before 023 is tag-free), so nothing can violate the
constraint at upgrade time.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '024'
down_revision: Union[str, None] = '023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint('ck_wallets_exchange_requires_memo', 'wallets',
                               "destination_type <> 'exchange' OR memo IS NOT NULL")
    op.create_check_constraint('ck_purchases_exchange_requires_memo', 'purchases',
                               "wallet_destination_type <> 'exchange' OR wallet_memo IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint('ck_purchases_exchange_requires_memo', 'purchases', type_='check')
    op.drop_constraint('ck_wallets_exchange_requires_memo', 'wallets', type_='check')
