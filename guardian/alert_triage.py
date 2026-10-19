"""Working the alert queue across every customer (G-37, #41).

The ops dashboard could show one account's alerts at a time. Nothing answered
"what is outstanding right now, everywhere" — so whether an alert got looked at
depended on somebody opening the right page, and an alert nobody opens is
indistinguishable from an alert nobody needed to.

## Triage is not acknowledgement, and must never become it

`Escalator` decides whether to escalate a CRITICAL by asking whether
`alerts.acknowledged_at IS NULL`. Acknowledgement is the *customer's* act: the
person whose money is moving has seen it.

If "mark as handled" wrote to that column, an operator ticking off their queue
would silently switch off the escalation ladder for a customer who has still
not responded — internal bookkeeping disabling the mechanism that exists for
exactly that case. So triage state lives in its own table and is invisible to
the escalator. A handled alert still escalates if the customer never
acknowledges it, which is the correct behaviour and worth the extra table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from guardian.db import Database
from guardian.events import append_event
from guardian.rules_schema import default_for

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stale_after_minutes(rules=None) -> int:
    """When an unacknowledged alert starts being highlighted as overdue.

    The same number the escalation ladder uses. An operator queue that called
    an alert overdue at a different point from the mechanism that escalates it
    would be teaching the operator a deadline the product does not keep.
    """
    if rules is not None:
        return rules.get_int("alerting", "escalation_unacknowledged_minutes")
    return int(default_for("alerting", "escalation_unacknowledged_minutes") or 15)


def open_alerts(db: Database, rules=None, now: datetime | None = None,
                include_handled: bool = False) -> list[dict]:
    """Every unacknowledged alert across every customer, worst first.

    "Unacknowledged" is the customer's state, so this is the queue of alerts
    where nobody whose money it is has responded yet. Ordered by severity and
    then by age, because a two-hour-old CRITICAL and a two-hour-old LOW are not
    the same backlog.
    """
    now = now or datetime.now(timezone.utc)
    overdue_after = timedelta(minutes=stale_after_minutes(rules))

    with db.cursor() as cur:
        cur.execute(
            "SELECT al.id, al.account_id, al.rule, al.severity, al.message, al.sent_at, "
            "       al.delivered_to, al.escalated_at, ac.label, "
            "       t.note, t.handled_at, t.handled_by "
            "FROM guardian_alerts al "
            "JOIN guardian_accounts ac ON ac.id = al.account_id "
            "LEFT JOIN guardian_alert_triage t ON t.alert_id = al.id "
            "WHERE al.acknowledged_at IS NULL "
            "ORDER BY al.sent_at DESC"
        )
        rows = cur.fetchall()

    out = []
    for r in rows:
        handled = r["handled_at"] is not None
        if handled and not include_handled:
            continue

        age_minutes = None
        try:
            age_minutes = int((now - datetime.fromisoformat(r["sent_at"])).total_seconds() // 60)
        except (ValueError, TypeError):
            age_minutes = None

        out.append({
            "alert_id": int(r["id"]),
            "account_id": int(r["account_id"]),
            "label": str(r["label"]),
            "rule": str(r["rule"]),
            "severity": str(r["severity"]),
            "message": str(r["message"]),
            "sent_at": r["sent_at"],
            "age_minutes": age_minutes,
            # Overdue is about the customer not having responded, so a handled
            # alert whose customer is still silent is still overdue. Ticking it
            # off our list does not answer for them.
            "overdue": age_minutes is not None and age_minutes >= overdue_after.total_seconds() / 60,
            "delivered_to": int(r["delivered_to"] or 0),
            "escalated": r["escalated_at"] is not None,
            "note": r["note"] or "",
            "handled": handled,
            "handled_by": r["handled_by"],
        })

    out.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), -(a["age_minutes"] or 0)))
    return out


def set_triage(db: Database, alert_id: int, *, note: str | None = None,
               handled: bool | None = None, actor: str | None = None) -> None:
    """Record a note and/or the handled marker against one alert.

    Upserts, because triage is current state rather than history. The durable
    record of who did what and when is the `events` row written alongside — the
    events table is append-only and is what an incident review reads.
    """
    now = _now_iso()
    with db.cursor() as cur:
        cur.execute("SELECT account_id FROM guardian_alerts WHERE id = %s", (alert_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"no such alert: {alert_id}")
        account_id = int(row["account_id"])

        cur.execute(
            "INSERT INTO guardian_alert_triage (alert_id, note, updated_at) VALUES (%s, '', %s) "
            "ON CONFLICT(alert_id) DO NOTHING",
            (alert_id, now),
        )
        if note is not None:
            cur.execute(
                "UPDATE guardian_alert_triage SET note = %s, updated_at = %s WHERE alert_id = %s",
                (note, now, alert_id),
            )
        if handled is not None:
            cur.execute(
                "UPDATE guardian_alert_triage SET handled_at = %s, handled_by = %s, updated_at = %s "
                "WHERE alert_id = %s",
                (now if handled else None, actor if handled else None, now, alert_id),
            )

    append_event(
        db, account_id, "alert_triaged", "LOW",
        {
            "alert_id": alert_id,
            "handled": handled,
            "note_set": note is not None,
            # The note itself goes in the event too: the triage row holds only
            # the latest, and "what did we think at the time" is the question an
            # incident review asks.
            "note": note if note is not None else None,
        },
        actor=actor,
    )


def muted_channels(db: Database, now: datetime | None = None) -> list[dict]:
    """Customers who have turned something off, and what.

    The early warning for a noise problem: somebody silencing alerts is telling
    you the alerts are not worth reading, and they say it to the product rather
    than to a person. Only worth surfacing while the silence is live, so an
    expired snooze does not sit here forever as a grievance.
    """
    now = now or datetime.now(timezone.utc)
    muted: dict[int, dict] = {}

    with db.cursor() as cur:
        cur.execute(
            "SELECT s.account_id, s.rule, s.expires_at, ac.label FROM guardian_snoozes s "
            "JOIN guardian_accounts ac ON ac.id = s.account_id WHERE s.expires_at > %s",
            (now.isoformat(),),
        )
        for r in cur.fetchall():
            entry = muted.setdefault(
                int(r["account_id"]),
                {"account_id": int(r["account_id"]), "label": str(r["label"]),
                 "snoozed_rules": [], "sms_off": False},
            )
            entry["snoozed_rules"].append(str(r["rule"]))

        cur.execute(
            "SELECT id, label FROM guardian_accounts WHERE sms_alerts_enabled = false"
        )
        for r in cur.fetchall():
            entry = muted.setdefault(
                int(r["id"]),
                {"account_id": int(r["id"]), "label": str(r["label"]),
                 "snoozed_rules": [], "sms_off": False},
            )
            entry["sms_off"] = True

    return sorted(muted.values(), key=lambda m: m["label"])
