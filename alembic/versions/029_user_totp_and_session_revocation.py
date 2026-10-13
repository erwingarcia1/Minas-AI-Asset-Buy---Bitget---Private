"""Two-factor authentication (TOTP) and sign out everywhere (v0.9.0, PR TBD)

user_totp: at most one enrolment per user. `encrypted_secret` is the TOTP
shared secret under the platform cipher (the same sha256-derived Fernet from
ENCRYPTION_KEY that protects exchange keys): a database read must not yield a
working authenticator. `confirmed` is false while enrolling - the secret is
stored so the customer can scan it, but 2FA is not in force until they have
proved they can produce a code, otherwise a mis-scanned QR locks them out.
`last_used_step` is replay protection: each 30-second step is accepted once.

user_totp_recovery_codes: single-use codes that bypass the second factor,
stored as sha256 of the normalised code (the scheme Guardian already uses, so
migrated codes keep working). `used_at` is set rather than the row deleted, so
a redeemed code stays distinguishable from one that never existed.

users.sessions_revoked_at: "sign out everywhere". Tokens issued before it are
refused by the same comparison that already applies to password_changed_at,
which keeps its own meaning (the password-changed email and the wallet hold
are tied to it). NULL = never used. No backfill.

Both tables start empty; nothing changes for a user who never enrols.

Revision ID: 029
Revises: 028
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '029'
down_revision: Union[str, None] = '028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_totp',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('encrypted_secret', sa.String(length=500), nullable=False),
        sa.Column('confirmed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('last_used_step', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
    )
    op.create_table(
        'user_totp_recovery_codes',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_user_totp_recovery_codes_user_used',
        'user_totp_recovery_codes',
        ['user_id', 'used_at'],
    )
    op.add_column('users', sa.Column('sessions_revoked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'sessions_revoked_at')
    op.drop_index('ix_user_totp_recovery_codes_user_used', table_name='user_totp_recovery_codes')
    op.drop_table('user_totp_recovery_codes')
    op.drop_table('user_totp')
