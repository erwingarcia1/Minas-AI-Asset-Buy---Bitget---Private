"""api_keys.permissions: the permission codes the exchange reported at verification

Revision ID: 020
Revises: 019
Create Date: 2026-09-11

Stored so the Settings page can warn when a key carries permissions AutoBuy
does not need (Bitget: margin trade, wallet transfer, taxation/P2P search).
Comma-separated codes, never credentials.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '020'
down_revision: Union[str, None] = '019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('api_keys', sa.Column('permissions', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('api_keys', 'permissions')
