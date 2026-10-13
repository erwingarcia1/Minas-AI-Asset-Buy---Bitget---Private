"""Who an account belongs to and where its alerts go.

Guardian tied an account to a person by string: `accounts.alert_email`
matched a portal member's email. In the platform an account has an owner,
`guardian_accounts.user_id`, and the owner's `users.email` is the primary
alert destination; the account keeps its own overrides (a phone, a secondary
email and phone) as delivery-only extras. Nothing else in the engine knows
about users: every contact lookup comes through here.
"""
from __future__ import annotations

from guardian.db import Database


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def accounts_for_user(db: Database, user_id) -> list:
    """Every monitored account this user owns, with its poll state. THE
    isolation boundary for anything per-user: callers scope their queries
    to the ids returned here."""
    if user_id is None:
        return []
    with db.cursor() as cur:
        cur.execute(
            "SELECT a.*, p.last_success_at, p.consecutive_failures, p.consecutive_auth_failures "
            "FROM guardian_accounts a LEFT JOIN guardian_poll_state p ON p.account_id = a.id "
            "WHERE a.user_id = %s ORDER BY a.label",
            (user_id,),
        )
        return cur.fetchall()


def account_contacts(db: Database, account_id: int) -> tuple[list[str], list[str]]:
    """(emails, phones) for an account: the owner's email first, then the
    secondary; the account's own phone (or the owner's verified number),
    then the secondary. Blank entries dropped, order kept."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT a.alert_phone, a.alert_email_secondary, a.alert_phone_secondary, "
            "u.email AS owner_email, u.phone_number AS owner_phone, u.phone_verified AS owner_phone_verified "
            "FROM guardian_accounts a JOIN users u ON u.id = a.user_id WHERE a.id = %s",
            (account_id,),
        )
        row = cur.fetchone()
    if row is None:
        return [], []
    owner_phone = row["owner_phone"] if row["owner_phone_verified"] else None
    emails = [row["owner_email"], row["alert_email_secondary"]]
    phones = [row["alert_phone"] or owner_phone, row["alert_phone_secondary"]]
    return [e for e in emails if e], [p for p in phones if p]
