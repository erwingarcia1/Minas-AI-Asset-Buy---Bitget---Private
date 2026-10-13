from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from guardian.alerts.base import AlertChannel, AlertMessage
from guardian.db import Database

logger = logging.getLogger("guardian.escalation")


class _EscalationDetection:
    """Adapts an escalation `AlertMessage` to the shape `ContactRouter.send`
    expects. The rule name keeps its ESCALATION prefix on purpose: someone
    reading this needs to know it is the second time, or it is
    indistinguishable from the message they already ignored."""

    def __init__(self, alert: AlertMessage):
        self.rule = alert.rule
        self.severity = alert.severity
        self.confidence = alert.confidence
        self.message = alert.message
        self.evidence = alert.evidence


class Escalator:
    """Re-fires CRITICAL alerts that nobody acknowledged within the
    configured window. Acknowledge with: sentinel ack <alert_id>."""

    def __init__(self, channels: list[AlertChannel], unacknowledged_minutes: int,
                 operator_alerter=None, contact_router=None):
        self._channels = channels
        self._window = timedelta(minutes=unacknowledged_minutes)
        # Optional: a deployment with no operator paging configured is worse
        # off, not broken. See `_page_if_nobody_heard`.
        self._operator_alerter = operator_alerter
        # The customer's OWN contacts, primary and secondary (G-21, #27).
        # Without this, escalation re-fired down the same global channels the
        # original alert used - the path that has already failed to get a
        # response - and the second pair of eyes never saw it. Escalation that
        # repeats the unanswered channel is not escalation.
        self._contact_router = contact_router

    def check(self, db: Database) -> int:
        cutoff = (datetime.now(timezone.utc) - self._window).isoformat()
        with db.cursor() as cur:
            cur.execute(
                "SELECT a.id, a.rule, a.severity, a.message, a.confidence, a.account_id, "
                "ac.label "
                "FROM guardian_alerts a JOIN guardian_accounts ac ON ac.id = a.account_id "
                "WHERE a.severity = 'CRITICAL' AND a.acknowledged_at IS NULL "
                "AND a.escalated_at IS NULL AND a.sent_at < %s "
                # A live snooze holds escalation too (G-26, #30). Escalating
                # past a snooze would page the customer anyway and make the
                # button a lie; suppressing escalation *permanently* would be
                # the permanent mute this product does not have. It holds, and
                # then it lets go - which is what snoozes expiring is for.
                "AND NOT EXISTS (SELECT 1 FROM guardian_snoozes s WHERE s.account_id = a.account_id "
                "AND s.dedup_key = a.dedup_key AND s.expires_at > %s)",
                (cutoff, datetime.now(timezone.utc).isoformat()),
            )
            stale = cur.fetchall()

        escalated = 0
        for row in stale:
            alert = AlertMessage(
                account_label=row["label"],
                rule=f"ESCALATION-unacknowledged: {row['rule']}",
                severity="CRITICAL",
                confidence=row["confidence"],
                message=(
                    f"CRITICAL alert #{row['id']} has not been acknowledged. "
                    f"Original: {row['message']} -- acknowledge with: sentinel ack {row['id']}"
                ),
                evidence={"original_alert_id": row["id"]},
            )
            delivered = 0
            for channel in self._channels:
                try:
                    channel.send(alert)
                    delivered += 1
                except Exception as exc:  # noqa: BLE001 - escalation must try every channel
                    logger.error("escalation channel failed: %s", exc)
            self._reach_the_customers_own_contacts(db, row, alert)
            self._page_escalated(row)
            self._page_if_nobody_heard(delivered, row)
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE guardian_alerts SET escalated_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc).isoformat(), row["id"]),
                )
            escalated += 1
        return escalated

    def _reach_the_customers_own_contacts(self, db, row, alert) -> None:
        """Deliver the escalation to the account's own email and phone,
        primary and secondary (#27).

        Guarded on its own, on the same rule as everything else on a delivery
        path (#57): the router reaching the customer's address and the global
        channels reaching whatever else is configured are two independent
        deliveries, and losing both because one raised is the failure this
        codebase keeps having."""
        if self._contact_router is None:
            return
        from guardian.contacts import account_contacts
        emails, phones = account_contacts(db, row["account_id"])
        if not emails and not phones:
            return
        try:
            self._contact_router.send(
                _EscalationDetection(alert), row["label"], emails, phones,
                account_id=row["account_id"], alert_id=row["id"],
            )
        except Exception as exc:  # noqa: BLE001 - one path must not cost the other
            logger.error("escalation to the account's own contacts failed: %s", exc)

    def _page_escalated(self, row) -> None:
        """Minas is told that an escalation happened at all (#27).

        Distinct from `escalation_delivered_to_nobody`, which is about our
        machinery failing. This one is about the *customer*: a CRITICAL has
        gone unanswered for the whole window, which is worth an operator's
        attention whether or not the escalation message left the building.
        Both fire when both are true."""
        if self._operator_alerter is None:
            return
        self._operator_alerter.page(
            rule="critical_alert_escalated",
            severity="HIGH",
            message=(
                f"CRITICAL alert #{row['id']} ({row['rule']}) on {row['label']} was not "
                f"acknowledged and has been escalated. The customer has not responded to a "
                f"CRITICAL for the full escalation window."
            ),
            evidence={"original_alert_id": row["id"], "rule": row["rule"],
                      "account_label": row["label"]},
            account_id=row["account_id"],
            dedup_scope=f"escalated:{row['id']}",
        )

    def _page_if_nobody_heard(self, delivered: int, row) -> None:
        """The strongest entry in the #58 sweep, and the only one with a real
        claim on CRITICAL.

        Escalation is what happens when a CRITICAL goes unacknowledged. There
        is nothing after it. If the escalation itself reaches nobody, the
        customer is not answering *and* nobody knows they are not answering -
        and the log line this used to be went into a file that, by definition,
        nobody was reading at the time.

        Only on zero. One channel failing while another delivers is degraded,
        not uninformed, and paging on that would train an operator to ignore
        the channel - which is the same outcome as not paging at all. An empty
        channel list counts as zero on purpose: every escalation going nowhere
        is worse than one that fails loudly."""
        if delivered > 0 or self._operator_alerter is None:
            return
        self._operator_alerter.page(
            rule="escalation_delivered_to_nobody",
            severity="CRITICAL",
            message=(
                f"Alert #{row['id']} ({row['rule']}) on {row['label']} went unacknowledged, "
                f"escalated, and the escalation reached no channel at all. Escalation is the "
                f"last resort - there is nothing after it. Check the channels configured for "
                f"escalation."
            ),
            evidence={"original_alert_id": row["id"], "rule": row["rule"],
                      "account_label": row["label"]},
            dedup_scope="escalation",
        )
