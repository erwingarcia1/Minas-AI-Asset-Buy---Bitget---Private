"""users.wallet_hold_until / wallet_hold_reason: 24-hour hold on new withdrawal addresses

Revision ID: 022
Revises: 021
Create Date: 2026-09-14

After a password reset or change, the account may not send coins anywhere
new for 24 hours: adding a wallet address to a purchase, switching a
purchase to withdraw-immediately, or creating an autobuy purchase to an
address this user has never used. Stored on the user row so it survives
restarts and is seen by every worker. NULL / past = no hold.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '022'
down_revision: Union[str, None] = '021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('wallet_hold_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('wallet_hold_reason', sa.String(length=40), nullable=True))  # password_reset | password_change


def downgrade() -> None:
    op.drop_column('users', 'wallet_hold_reason')
    op.drop_column('users', 'wallet_hold_until')
