from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from guardian.alerts.base import AlertChannel, AlertMessage
from guardian.db import Database
from guardian.detectors.base import Detection
from guardian.snooze import active_snooze

logger = logging.getLogger("guardian.alerts.dispatch")


# Evidence fields that identify *which* occurrence of a rule this is, in
# preference order. A detection is deduped against others sharing its rule,
# account and identity - so anything that distinguishes two real occurrences has
# to appear here.
#
# `symbol` is why this is a list rather than the previous
# `order_id or coin or ""`. An off-market fill carries `symbol`, not `coin`, and
# its `order_id` is None when the exchange didn't reference the fill - so the
# identity collapsed to the empty string and *every* off-market fill on an
# account shared one dedup key. The second symbol's alert was suppressed for the
# whole HIGH cooldown. G-47 is what made that reachable: before it, id-less
# fills were never judged at all, so they never got as far as being deduped.
_IDENTITY_FIELDS = ("order_id", "symbol", "coin", "address", "route")


def _dedup_key(account_id: int, detection: Detection) -> str:
    """Stable across repeated firings of the 'same' underlying condition
    (same rule + same evidence identity) so a flapping condition doesn't storm
    the alert channel - while two genuinely different occurrences of one rule
    stay two alerts.

    Absent fields are skipped rather than substituted with a falsy value: an
    identity of `""` is not an identity, and treating it as one is how distinct
    events collapse into a single suppressed alert. When a detection genuinely
    carries nothing identifying, rule + account is the honest key - that is one
    condition, not many."""
    identity_bits = ":".join(
        f"{field}={detection.evidence[field]}"
        for field in _IDENTITY_FIELDS
        if detection.evidence.get(field) is not None
    )
    raw = f"{account_id}:{detection.rule}:{identity_bits}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def dispatch(
    db: Database,
    channels: dict[str, list[AlertChannel]],
    account_id: int,
    account_label: str,
    detection: Detection,
    cooldown_minutes: dict[str, int],
    rule_cooldowns: dict[str, int] | None = None,
) -> int | None:
    """Sends `detection` to the channels registered for its severity, unless
    an alert with the same dedup key already fired within the cooldown
    window for that severity. Returns the stored alert's row id when an
    alert was actually sent (so downstream delivery logs can reference it),
    or None when it was deduped away."""
    dedup_key = _dedup_key(account_id, detection)
    # A per-rule override beats the severity default. Severity answers "how
    # urgent is this"; some rules also need "and how often may it repeat",
    # which is a different question. `account_unreachable` is the case that
    # forced it: at HIGH's 30 minutes a broken key would page twice an hour
    # until fixed, which trains a customer to ignore it, and with no repeat at
    # all an unwatched account looks fine forever (G-6).
    minutes = (rule_cooldowns or {}).get(detection.rule, cooldown_minutes.get(detection.severity, 60))
    cooldown = timedelta(minutes=minutes)
    now = datetime.now(timezone.utc)

    with db.cursor() as cur:
        cur.execute(
            "SELECT sent_at FROM guardian_alerts WHERE account_id = %s AND dedup_key = %s "
            "ORDER BY sent_at DESC LIMIT 1",
            (account_id, dedup_key),
        )
        row = cur.fetchone()

    if row is not None:
        last_sent = datetime.fromisoformat(row["sent_at"])
        if now - last_sent < cooldown:
            return None

    # A live snooze withholds the delivery and nothing else: the detection has
    # already run, and the alerts row below is written either way. The customer
    # silenced their phone, not the monitoring - so the history a customer
    # hands to an exchange still has the event in it, marked as silenced on
    # request rather than missing.
    snoozed_until = active_snooze(db, account_id, dedup_key)

    alert = AlertMessage(
        account_label=account_label,
        rule=detection.rule,
        severity=detection.severity,
        confidence=detection.confidence,
        message=detection.message,
        evidence=detection.evidence,
    )
    # Each send is a network call - Telegram HTTP, SMTP, an SMS provider - and
    # each is guarded on its own. Unguarded, one flaky channel skipped every
    # channel after it (including the ones configured as backups), left no
    # `alerts` row at all, and threw far enough to abandon the rest of that
    # account's poll cycle (#57).
    delivered = 0
    for channel in ([] if snoozed_until else channels.get(detection.severity, [])):
        try:
            channel.send(alert)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - a dead channel must not cost the others
            logger.error(
                "alert channel failed (%s) for %s on %s: %s",
                type(channel).__name__, detection.rule, account_label, exc,
            )

    # Recorded whether or not anyone took it. "We detected this" and "we managed
    # to tell someone" are different facts, and sharing a row's existence made
    # an undelivered alert indistinguishable from one that never happened - so
    # it was absent from the customer's history, the portal, the dashboard, and
    # anything handed to an exchange as evidence. `delivered_to` is what keeps
    # them apart, on the same footing as operator_pages.
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO guardian_alerts
                (account_id, rule, severity, dedup_key, message, evidence_json, confidence,
                 sent_at, delivered_to, snoozed_until)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                account_id,
                detection.rule,
                detection.severity,
                dedup_key,
                detection.message,
                json.dumps(detection.evidence, default=str),
                detection.confidence,
                now.isoformat(),
                delivered,
                snoozed_until,
            ),
        )
        alert_id = cur.fetchone()["id"]
    return alert_id
