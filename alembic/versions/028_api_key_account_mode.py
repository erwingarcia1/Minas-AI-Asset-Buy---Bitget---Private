"""api_keys.account_mode: which Bitget API family the key's account speaks (v0.8.0, PR TBD)

classic  = Classic account, v2 endpoints (every key verified before 2026-10-08 behaves this way)
unified  = Unified Trading Account, v3 endpoints
unknown  = not yet detected (a key never verified, or verified before this column existed and not rechecked)

Backfill: every key 'unknown' (owner's decision, 2026-10-08). Verification and the daily check set
the real mode; the executor treats unknown as Classic and switches a key to unified on 40085 only.

Revision ID: 028
Revises: 027
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '028'
down_revision: Union[str, None] = '027'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('api_keys', sa.Column('account_mode', sa.String(length=10), nullable=False, server_default='unknown'))
    op.create_check_constraint('ck_api_keys_account_mode', 'api_keys', "account_mode IN ('classic', 'unified', 'unknown')")


def downgrade() -> None:
    op.drop_constraint('ck_api_keys_account_mode', 'api_keys', type_='check')
    op.drop_column('api_keys', 'account_mode')
