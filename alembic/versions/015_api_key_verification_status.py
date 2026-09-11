"""api key verification status, error and timestamps

Revision ID: 015
Revises: 014
Create Date: 2026-09-11

api_keys only had a boolean ``verified``, so a failed verification was
indistinguishable from one that never ran. Adds verification_status
(pending | verified | failed), a readable verification_error (never contains
credentials), verified_at, last_checked_at and withdrawal_enabled (what the
exchange reported about the key's permissions). Existing rows are backfilled
from the boolean.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '015'
down_revision: Union[str, None] = '014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('api_keys', sa.Column('verification_status', sa.String(length=20), nullable=False,
                                        server_default='pending'))
    op.add_column('api_keys', sa.Column('verification_error', sa.String(length=500), nullable=True))
    op.add_column('api_keys', sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('api_keys', sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('api_keys', sa.Column('withdrawal_enabled', sa.Boolean(), nullable=True))
    op.execute("UPDATE api_keys SET verification_status = 'verified', verified_at = updated_at, "
               "last_checked_at = updated_at WHERE verified = true")
    op.execute("UPDATE api_keys SET verified = false WHERE verified IS NULL")
    op.alter_column('api_keys', 'verified', existing_type=sa.Boolean(), nullable=False, server_default='false')


def downgrade() -> None:
    op.alter_column('api_keys', 'verified', existing_type=sa.Boolean(), nullable=True, server_default=None)
    for col in ('withdrawal_enabled', 'last_checked_at', 'verified_at', 'verification_error', 'verification_status'):
        op.drop_column('api_keys', col)
