"""Paging scoped to Minas rather than to a customer (G-49).

The gap this closes was never "we have no way to reach ourselves" -
`build_channels` already wires our own Telegram, the team group, our SMS
numbers and our email. It was that every route ran through an account:
`dispatch()` takes an `account_id`, `_dedup_key` is built from it, and the
`alerts` row carries it as a foreign key. So a product-level fault had nowhere
to be raised, and anything raised against a real account went on through
`AccountPoller._emit` to `ContactRouter.send` and told that customer about our
problem.

Two rules hold everywhere in this module:

**`account_id` means "about", never "to".** A page may name the account it
concerns, and still never reaches that customer.

**There is no customer path here at all.** Not a disabled one, not a
conditional one - `OperatorAlerter` has no ContactRouter and no way to acquire
one. A route that must never fire is safest when it does not exist.

Scope, worth restating from the issue so this is never mistaken for more than
it is: this is the product reporting its own faults *while it is running*. It
cannot report the process or the machine dying, because nothing in-process can,
and an alarm that depends on the thing it monitors is not an alarm. Closing
G-49 does not mean we would know if Guardian stopped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from guardian.alerts.base import AlertMessage
from guardian.db import Database

logger = logging.getLogger("guardian.alerts.operator")

# Used as the alert's account label so an operator page is unmistakable in a
# Telegram thread that also carries customer alerts.
OPERATOR_LABEL = "MINAS-OPS"



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedup_key(rule: str, account_id: int | None, dedup_scope: str | None = None) -> str:
    """Scoped by rule, by the account it is about, and by whatever else makes
    two occurrences of that rule distinct.

    The account has to be in the key. Two accounts going unwatchable are two
    facts, and collapsing them would report the first and hide the rest of a
    fleet-wide outage - which is the exact shape of problem this exists to
    catch.

    `dedup_scope` is the same argument for pages that aren't about an account
    at all. "Binance stopped stamping fills" and "Bitget stopped stamping
    withdrawals" are one rule with `account_id=None` twice, and without a scope
    they collapse into one page - reporting the first exchange and hiding the
    second. Callers pass whatever their own unit of distinctness is."""
    raw = f"operator:{rule}:{account_id if account_id is not None else '-'}:{dedup_scope or '-'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class OperatorAlerter:
    """Raises pages to Minas. Never to a customer.

    `channels` is a severity -> [channel] map built from the
    `operator_alerting.routing` section, which is separate from the customer
    `alerting.routing` table on purpose: "our product is broken" and "this
    customer may be being robbed" are different kinds of urgency and need
    different dials."""

    def __init__(self, db: Database, channels: dict, rules):
        self._db = db
        self._channels = channels or {}
        self._rules = rules

    def _cooldown(self, severity: str) -> timedelta:
        minutes = self._rules.get_int("operator_alerting", "cooldown_minutes", severity)
        return timedelta(minutes=minutes)

    def page(
        self,
        rule: str,
        severity: str,
        message: str,
        evidence: dict | None = None,
        account_id: int | None = None,
        dedup_scope: str | None = None,
    ) -> int | None:
        """Raises an operator page. Returns the stored row id, or None if an
        identical page fired inside the cooldown.

        `account_id` names the account a page is *about*. It never causes the
        customer to be notified.

        `dedup_scope` separates two occurrences of one rule that aren't
        distinguished by the account - see `_dedup_key`."""
        evidence = dict(evidence or {})
        key = _dedup_key(rule, account_id, dedup_scope)
        cooldown = self._cooldown(severity)
        now = _now()

        with self._db.cursor() as cur:
            cur.execute(
                "SELECT sent_at FROM guardian_operator_pages WHERE dedup_key = %s ORDER BY sent_at DESC LIMIT 1",
                (key,),
            )
            row = cur.fetchone()
        if row is not None and now - datetime.fromisoformat(row["sent_at"]) < cooldown:
            logger.debug("operator page %s suppressed by cooldown", rule)
            return None

        alert = AlertMessage(
            account_label=OPERATOR_LABEL if account_id is None else f"{OPERATOR_LABEL} · account {account_id}",
            rule=rule,
            severity=severity,
            confidence="operator",
            message=message,
            evidence=evidence,
        )

        delivered = 0
        for channel in self._channels.get(severity, []):
            try:
                channel.send(alert)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - one dead channel must not lose the page
                logger.error("operator page channel failed (%s): %s", type(channel).__name__, exc)

        if delivered == 0:
            # Recorded anyway. A page that could not be delivered must not also
            # be a page that never happened - the row is what makes a
            # misconfigured route discoverable afterwards instead of silently
            # swallowed.
            logger.error(
                "operator page '%s' (%s) had no deliverable channel: %s",
                rule, severity, message,
            )

        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO guardian_operator_pages "
                "(account_id, rule, severity, dedup_key, message, evidence_json, delivered_to, sent_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (account_id, rule, severity, key, message,
                 json.dumps(evidence, default=str), delivered, now.isoformat()),
            )
            return cur.fetchone()["id"]


def recent_operator_pages(db: Database, limit: int = 50) -> list:
    """Most recent pages, newest first, with the account label where a page is
    about one. This is what makes "what broke last month" answerable."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT o.*, a.label AS account_label FROM guardian_operator_pages o "
            "LEFT JOIN guardian_accounts a ON a.id = o.account_id "
            "ORDER BY o.sent_at DESC, o.id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def acknowledge_operator_page(db: Database, page_id: int) -> bool:
    """Marks a page as handled. Returns False if it was already acknowledged,
    so an ack is recorded once and keeps its original time."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE guardian_operator_pages SET acknowledged_at = %s WHERE id = %s AND acknowledged_at IS NULL",
            (_now().isoformat(), page_id),
        )
        return cur.rowcount == 1
