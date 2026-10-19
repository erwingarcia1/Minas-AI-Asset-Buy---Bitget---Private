from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from api.services.telegram import mask_email
from guardian.db import Database

logger = logging.getLogger("guardian.events")


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def append_event(
    db: Database, account_id: int, event_type: str, severity: str, payload: dict,
    actor: str | None = None,
) -> int:
    """Writes one row to the append-only event log. The application must
    never UPDATE or DELETE the events table - it is the forensic record that
    baselines and alert evidence are derived from.

    `actor` is the operator username where a person did this, and None where
    nothing did - a detector firing has nobody behind it (G-50). It stays
    nullable for exactly that reason: filling it in with a guess would make the
    log say a person acted when none did, which is worse than saying nothing."""
    now = datetime.now(timezone.utc).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (account_id, event_type, severity, json.dumps(payload, cls=_DecimalEncoder), now, actor),
        )
        return cur.fetchone()["id"]


# Event types that are a change to how an account is watched or reached, as
# opposed to something a detector saw. `/activity` in the portal shows exactly
# these (G-32, #36): a customer asking "who has touched my account" is asking
# about settings and administration, and mixing detections in would bury the
# one page where an unauthorised change is supposed to stand out.
#
# A allowlist rather than a denylist of detector rules. Detector rules are
# added often and this list is not, so a denylist would silently start showing
# every new detection here - and a page that fills with noise is a page nobody
# reads, which is how an unauthorised change goes unnoticed.
ADMIN_EVENT_TYPES = (
    "customer_added",
    "alert_contact_updated",
    "sms_alerts_changed",
    "member_portal_password_generated",
    "member_password_changed",
    "member_two_factor_enabled",
    "member_two_factor_disabled",
    "monitoring_key_replaced",
    "account_enabled",
    "account_disabled",
    "account_deleted",
    "baseline_seed_failed",
)

# How each reads to a customer. The dashboard shows event_type; this page is
# read by somebody deciding whether to phone their exchange, so it says what
# happened in words.
ADMIN_EVENT_LABELS = {
    "customer_added": "Account added to monitoring",
    "alert_contact_updated": "Alert destination changed",
    "sms_alerts_changed": "SMS alert preference changed",
    "member_portal_password_generated": "Portal password issued by Minas",
    "member_password_changed": "Portal password changed",
    "member_two_factor_enabled": "Two-factor turned on",
    "member_two_factor_disabled": "Two-factor turned off",
    "monitoring_key_replaced": "Monitoring key replaced",
    "account_enabled": "Monitoring resumed",
    "account_disabled": "Monitoring paused",
    "account_deleted": "Account removed from monitoring (history kept)",
    "baseline_seed_failed": "Baseline could not be read from the exchange",
}


def notify_alert_destination_change(db: Database, account_label: str,
                                    old_email: str | None, new_email: str | None) -> None:
    """Tell the OLD address that the alerts have been redirected (G-32, #36).

    This is the one settings change the activity page cannot show the person
    who most needs it. Portal scope is the contact email, so the moment the
    destination changes, the previous contact stops seeing that account at all
    - including the record of the change that removed them. An attacker who
    redirects the alerts silently removes the evidence from the only page
    built to reveal it.

    So the notice goes to the address being removed, the way the
    password-change tripwire does. Not a receipt: it is the last message that
    address will ever get about this account, and it is sent precisely because
    somebody may not have authorised it.

    Guarded like every other notice on a change path: a mail failure is logged,
    never raised. The change has already happened, and an exception here would
    make a dead SMTP server look like a failed settings update."""
    if not old_email or old_email == new_email:
        return
    from guardian.alerts.base import AlertMessage, _PlainNotice
    from guardian.alerts.contact import ContactRouter

    destination = new_email or "no email address"
    message = AlertMessage(
        account_label=account_label,
        rule="alert_destination_changed",
        severity="LOW",
        confidence="n/a",
        message=(
            f"The alert destination for '{account_label}' was changed from {old_email} to "
            f"{destination}. Alerts about this account will no longer be sent to this address, "
            f"and this address can no longer see the account in the Minas portal.\n\n"
            f"If you did not ask for this, contact Minas immediately - redirecting the alerts "
            f"is what somebody does before moving the funds."
        ),
        evidence={},
    )
    try:
        ContactRouter(db=db).send(
            _PlainNotice(message), account_label, [old_email], [],
        )
    except Exception as exc:  # noqa: BLE001 - the change has already happened
        logger.error("could not send the destination-change notice to %s: %s", mask_email(old_email), exc)


def record_member_security_event(db: Database, user_id, event_type: str,
                                 payload: dict | None = None, *, login: str | None = None) -> int:
    """Record a change to a member's LOGIN against every account that login
    opens. Returns the number of rows written (G-32, #36).

    A password change or a second factor being removed is not a change to an
    account, and it is the single most useful line on "who has touched my
    account" - it is what an attacker does after getting in and before doing
    anything else. `events.account_id` is NOT NULL because the log is
    per-account, so this fans out rather than relaxing the schema: one row per
    account the login reaches, which is also the honest scope, since that is
    exactly what the change affects.

    Nothing is written when the address opens no accounts. A login with no
    accounts has nowhere to record this, and inventing a row would be
    inventing an account."""
    from guardian.contacts import accounts_for_user, normalize_email

    written = 0
    actor = normalize_email(login)
    for account in accounts_for_user(db, user_id):
        append_event(db, account["id"], event_type, "LOW",
                     {**(payload or {}), "login": actor},
                     actor=actor or None)
        written += 1
    return written


def record_config_changes(db: Database, changes, actor: str | None = None) -> int:
    """One row per changed threshold (G-12, #16). Returns the number written.

    `actor` is the operator who made the change where the change came through a
    path that carries an identity, and None where it did not. A file edited on
    a server carries no identity, and `None` is the truthful answer - see the
    note on `config_changes` in schema.sql for why it is not filled in."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [(setting, before, after, actor, now) for setting, before, after in changes]
    if not rows:
        return 0
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO guardian_config_changes (setting, old_value, new_value, actor, changed_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
    for setting, before, after in changes:
        logger.info(
            "config change: %s %s -> %s (%s)",
            setting, before, after, actor or "unattributed - edited on disk",
        )
    return len(rows)
