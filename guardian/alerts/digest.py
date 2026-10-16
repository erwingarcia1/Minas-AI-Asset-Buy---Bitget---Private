from __future__ import annotations

from datetime import datetime, timedelta, timezone

from guardian.alerts.base import AlertChannel, AlertMessage
from guardian.db import Database


def build_digest(db: Database, hours: int = 24) -> str | None:
    """Aggregates MEDIUM/LOW alerts from the window into one report.
    Returns None when there is nothing to say - no empty digests."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "SELECT a.severity, a.rule, a.message, a.sent_at, ac.label "
            "FROM guardian_alerts a JOIN guardian_accounts ac ON ac.id = a.account_id "
            "WHERE a.severity IN ('MEDIUM', 'LOW') AND a.sent_at >= %s "
            "ORDER BY ac.label, a.sent_at",
            (cutoff,),
        )
        rows = cur.fetchall()
    if not rows:
        return None

    lines = [f"Guardian daily digest - {len(rows)} MEDIUM/LOW event(s) in the last {hours}h", ""]
    current_label = None
    for row in rows:
        if row["label"] != current_label:
            current_label = row["label"]
            lines.append(f"— {current_label} —")
        lines.append(f"  [{row['severity']}] {row['rule']} @ {row['sent_at'][:16]}: {row['message']}")
    return "\n".join(lines)


def send_digest(db: Database, channels: list[AlertChannel], hours: int = 24) -> bool:
    body = build_digest(db, hours)
    if body is None:
        return False
    alert = AlertMessage(
        account_label="all accounts",
        rule="daily_digest",
        severity="LOW",
        confidence="n/a",
        message=body,
        evidence={},
    )
    for channel in channels:
        channel.send(alert)
    return True
