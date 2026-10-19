"""A customer's monitored accounts, as the API manages them (v0.9.2, PR3).

The engine owns every business rule about an account; the API routes call
these functions through `api/services/guardian_bridge.py` and map the results
to HTTP. Each function speaks raw SQL through `db.cursor()` like the rest of
the package and records what a person did as an append-only event
(`guardian.events.append_event`), because "who touched my account" is the one
page an unauthorised change must not be able to hide from.

Every dict returned here is the `guardian_accounts` row plus
`guardian_poll_state.last_success_at` (`None` until the first successful
poll). Timestamps are ISO-8601 strings in UTC, as `guardian.db` returns them.
The encrypted columns and the key fingerprint ARE in the dict, because the
row is; the API's response model must never echo them.

A removed account (`scrub_and_delete`) keeps its row and its history - the
alerts and events are the customer's forensic record and the engine's dedup
keys point at the id - but it is no longer theirs to see or act on: the
listing functions leave it out, `count_live_accounts` does not count it, and
it cannot be switched back on. Adding the account again is a new onboarding
with a new key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from api.services.telegram import mask_email
from guardian.db import Database
from guardian.events import append_event

logger = logging.getLogger("guardian.accounts")

# The fields a customer may change after onboarding, and the column widths
# (migration 030) a value must fit. `sms_alerts_enabled` is the one boolean;
# the rest are delivery-only overrides (the owner's `users.email` is always
# the primary destination and is not changed here).
CONTACT_FIELDS = (
    "alert_phone", "alert_email_secondary", "alert_phone_secondary", "telegram_handle", "sms_alerts_enabled",
)
_TEXT_LIMITS = {"alert_phone": 32, "alert_email_secondary": 255, "alert_phone_secondary": 32, "telegram_handle": 64}
_FIELD_WORDS = {
    "alert_phone": "phone number", "alert_email_secondary": "second email address",
    "alert_phone_secondary": "second phone number", "telegram_handle": "Telegram handle",
}

# A customer switches monitoring off and on; `tamper_suspected` is the
# engine's to set.
CUSTOMER_STATUSES = ("active", "disabled")

_SELECT = (
    "SELECT a.*, p.last_success_at FROM guardian_accounts a "
    "LEFT JOIN guardian_poll_state p ON p.account_id = a.id "
)


class NoSuchAccount(LookupError):
    """The id names no row at all. Callers check ownership first
    (`account_for_user`), so reaching this is a programming error, not an
    outcome a customer sees."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor_for_log(actor: str | None) -> str:
    if not actor:
        return "nobody"
    return mask_email(actor) if "@" in actor else actor


def _row(db: Database, account_id) -> dict | None:
    """The row whatever its state, for the functions that act on one."""
    with db.cursor() as cur:
        cur.execute(_SELECT + "WHERE a.id = %s", (account_id,))
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _existing_row(db: Database, account_id) -> dict:
    row = _row(db, account_id)
    if row is None:
        raise NoSuchAccount(f"no monitored account with id {account_id}")
    return row


def _is_removed(row: dict) -> bool:
    return row["encrypted_api_key"] == ""


def accounts_for_user(db: Database, user_id) -> list[dict]:
    """Every account this user owns that has not been removed, oldest first,
    each with its poll state's `last_success_at`."""
    if user_id is None:
        return []
    with db.cursor() as cur:
        cur.execute(
            _SELECT + "WHERE a.user_id = %s AND a.encrypted_api_key <> '' ORDER BY a.onboarded_at, a.id",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def account_for_user(db: Database, user_id, account_id) -> dict | None:
    """One account, or None when the row is not this user's, has been
    removed, or does not exist. The three cases are deliberately one answer:
    a caller turns None into "not found" and learns nothing else."""
    if user_id is None or account_id is None:
        return None
    with db.cursor() as cur:
        cur.execute(
            _SELECT + "WHERE a.id = %s AND a.user_id = %s AND a.encrypted_api_key <> ''",
            (account_id, user_id),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def count_live_accounts(db: Database, user_id) -> int:
    """How many accounts this user has that still hold a key: active,
    disabled or tamper-suspected, but not removed. This is what the ten-
    account cap counts."""
    if user_id is None:
        return 0
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM guardian_accounts WHERE user_id = %s AND encrypted_api_key <> ''",
            (user_id,),
        )
        return int(cur.fetchone()["n"])


def _clean(name: str, value):
    """A submitted contact value as it is stored: stripped, blank -> NULL,
    bounded to the column. A value that does not fit raises ValueError with
    a sentence a customer can act on."""
    if name == "sms_alerts_enabled":
        if not isinstance(value, bool):
            raise ValueError("sms_alerts_enabled must be true or false")
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if name == "alert_email_secondary":
        text = text.lower()
    if len(text) > _TEXT_LIMITS[name]:
        raise ValueError(f"The {_FIELD_WORDS[name]} is too long: it can be at most {_TEXT_LIMITS[name]} characters.")
    return text


def update_contacts(db: Database, account_id, *, actor: str | None, **fields) -> dict:
    """Change where this account's alerts go. `fields` are among
    CONTACT_FIELDS; anything else is a programming error (ValueError).

    Only fields whose value actually changes are written, and the
    'alert_contact_updated' event names those FIELDS and nothing else: the
    event log is readable in the portal and kept for years, and a phone
    number or an address does not belong in it. Nothing changed means no
    event, because a line saying "the alert destination changed" when it did
    not is exactly the noise that lets a real change hide."""
    unknown = sorted(set(fields) - set(CONTACT_FIELDS))
    if unknown:
        raise ValueError(f"unknown contact field(s): {', '.join(unknown)}")
    current = _existing_row(db, account_id)
    changes = {}
    for name, value in fields.items():
        cleaned = _clean(name, value)
        if cleaned != current[name]:
            changes[name] = cleaned
    if not changes:
        return current
    names = sorted(changes)
    assignments = ", ".join(f"{name} = %s" for name in names)   # names come from the allowlist above
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE guardian_accounts SET {assignments}, updated_at = %s WHERE id = %s",
            (*[changes[name] for name in names], _now(), account_id),
        )
    append_event(db, account_id, "alert_contact_updated", "LOW", {"fields": names}, actor=actor)
    logger.info("account %s: contact fields changed by %s: %s", current["label"], _actor_for_log(actor), ", ".join(names))
    return _existing_row(db, account_id)


def set_status(db: Database, account_id, status: str, *, actor: str | None) -> dict:
    """Switch monitoring off ('disabled') or back on ('active'). The engine
    polls only active rows, so disabling stops the next poll; enabling makes
    the account eligible again on the next pass, with its baseline as it was.

    A removed account cannot be re-enabled: there is no key to poll with.
    Setting the status the row already has writes nothing and records
    nothing, for the same reason `update_contacts` records only changes."""
    if status not in CUSTOMER_STATUSES:
        raise ValueError(f"status must be one of {', '.join(CUSTOMER_STATUSES)}, not {status!r}")
    current = _existing_row(db, account_id)
    if status == "active" and _is_removed(current):
        raise ValueError("This account was removed and cannot be switched back on. Add it again with a new read-only key.")
    if current["status"] == status:
        return current
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET status = %s, updated_at = %s WHERE id = %s",
                    (status, _now(), account_id))
    enabling = status == "active"
    # Switching monitoring off is the kind of change the activity page exists
    # to make visible, so it carries the weight a key replacement does.
    append_event(
        db, account_id, "account_enabled" if enabling else "account_disabled", "LOW" if enabling else "HIGH",
        {"from": current["status"], "to": status}, actor=actor,
    )
    logger.info("account %s: %s by %s", current["label"], "enabled" if enabling else "disabled", _actor_for_log(actor))
    return _existing_row(db, account_id)


def scrub_and_delete(db: Database, account_id, *, actor: str | None) -> dict:
    """Remove an account the way a customer means it: monitoring stops, the
    key material is gone, the history stays.

    The row is kept, disabled, with the three encrypted columns blanked and
    the fingerprint cleared (so the same key may be onboarded again later).
    Its alerts and events are left exactly as they were: they are the
    record of what was seen on this account, and a deletion that took the
    evidence with it would be the first thing an intruder asked for.
    Removing an account that is already removed changes and records nothing."""
    current = _existing_row(db, account_id)
    if _is_removed(current) and current["status"] == "disabled":
        return current
    with db.cursor() as cur:
        cur.execute(
            "UPDATE guardian_accounts SET status = 'disabled', encrypted_api_key = '', encrypted_secret = '', "
            "encrypted_passphrase = '', key_fingerprint = NULL, updated_at = %s WHERE id = %s",
            (_now(), account_id),
        )
    append_event(
        db, account_id, "account_deleted", "HIGH",
        {"label": current["label"], "exchange": current["exchange"], "previous_status": current["status"]},
        actor=actor,
    )
    logger.info("account %s: removed by %s; key material scrubbed, history kept", current["label"], _actor_for_log(actor))
    return _existing_row(db, account_id)
