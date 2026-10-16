"""Silencing one alert, for a while (G-26, #30).

This is the only feature here whose purpose is to stop an alert reaching
someone. Dedup and cooldown also suppress, but they suppress a *repeat* of
something already delivered; a snooze suppresses a delivery outright, on
request, at the moment a customer is least likely to be sure what is happening
to their account. Three properties keep that from becoming a way to turn the
product off:

**It is keyed on the alert, not the rule.** `dedup_key` already identifies "the
same underlying condition" — rule plus the evidence identity (order id,
address, symbol, coin, route) plus the account. Snoozing on that key means a
withdrawal to a *different* address is a different alert and stays loud, which
is the case that matters: the customer recognising one withdrawal is exactly
when the attacker's next one arrives.

**It always expires.** `expires_at` is NOT NULL and the duration is clamped
here rather than in the form, so the table cannot represent a permanent mute
even if every caller is wrong. A UI cap is not a cap — the form is a POST that
can be retyped.

**It silences the phone, not the monitoring.** A snoozed detection still runs,
still writes its `alerts` row, and still appears in the portal. Only the
delivery is withheld, and the row records that it was withheld *on request* —
which is a different fact from `delivered_to = 0`, meaning we tried to tell
someone and reached nobody (#57). Sharing one column would turn every snooze
into a false report of a broken alert channel.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from guardian.db import Database

logger = logging.getLogger("guardian.snooze")

# The longest a snooze may run, per severity (decision 8, answered
# 2026-08-13). Overridable under `alerting.snooze_cap_hours`.
#
# The reasoning, because the numbers are meaningless without it: acknowledging
# already stops escalation and already says "I have seen this". A snooze only
# suppresses *repeats*, so the only legitimate reason to suppress repeats of a
# CRITICAL is that you are dealing with it right now - which is a short window
# by definition. A drain unfolds over minutes to hours, so a 24-hour CRITICAL
# snooze is long enough for the rest of one to finish in silence. Re-snoozing
# costs one tap and forces a fresh decision, which is the right friction to put
# in front of continuing to ignore a CRITICAL.
#
# The 24 on MEDIUM and LOW is the digest cadence: no snooze outlives the next
# summary the customer receives.
SNOOZE_CAP_HOURS = {"CRITICAL": 1, "HIGH": 4, "MEDIUM": 24, "LOW": 24}

# A severity this code does not recognise gets the *shortest* cap, not the
# longest. For a feature whose purpose is suppression, the only safe direction
# to fail in is towards less of it.
UNKNOWN_SEVERITY_CAP_HOURS = min(SNOOZE_CAP_HOURS.values())

# Still the ceiling over every severity - the "no permanent mute" bound.
MAX_SNOOZE_HOURS = max(SNOOZE_CAP_HOURS.values())

# Durations offered in the portal, filtered by the severity's cap. Offering a
# 24h option on a CRITICAL that would be silently clamped to 1h tells the
# customer something untrue about what they just did.
_OFFERED_HOURS = (1, 4, 24)


def cap_hours(severity: str | None, rules=None) -> float:
    """The ceiling for this severity. `rules` overrides the built-in table so
    the caps are tunable without a deploy (G-12) and every change to them is
    recorded against a name (#16)."""
    if rules is not None and severity:
        # `is not None`, not truthiness. A configured cap of 0 means "this
        # severity cannot be snoozed at all" - decision 8's option 3 - and
        # `if configured:` would read that deliberate zero as "not set" and
        # hand back the built-in cap instead. That is the falsy-sentinel
        # pattern in CONTRIBUTING.md, and it would silently re-enable exactly
        # the suppression an owner had turned off.
        configured = rules.get("alerting", "snooze_cap_hours", severity, default=None)
        if configured is not None:
            return float(configured)
    if severity not in SNOOZE_CAP_HOURS:
        return float(UNKNOWN_SEVERITY_CAP_HOURS)
    return float(SNOOZE_CAP_HOURS[severity])


def snooze_choices(severity: str | None, rules=None) -> tuple:
    """Durations worth offering for this severity - never more than its cap,
    and never empty."""
    cap = cap_hours(severity, rules)
    if cap <= 0:
        # This severity cannot be snoozed. Empty, not `(0,)` - the `or (cap,)`
        # fallback below exists so an unusually small cap still offers itself,
        # and without this guard it would dutifully offer a "0h" button that
        # `snooze_alert` then refuses.
        return ()
    offered = tuple(h for h in _OFFERED_HOURS if h <= cap)
    return offered or (cap,)


def snooze_alert(
    db: Database, account_id: int, dedup_key: str, rule: str, hours: float, requested_by: str,
    severity: str | None = None, rules=None,
):
    """Record a snooze and return its row, or None if `hours` isn't a duration.

    A zero or negative duration is rejected rather than clamped up to a
    minimum. "Snooze for no time" is not a request this product needs to
    honour, and letting it through writes a row that is already expired -
    harmless today, and exactly the sort of already-in-the-past row that reads
    as a mute the moment some comparison is written the other way round."""
    try:
        hours = min(float(hours), cap_hours(severity, rules))
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        # Two ways to land here, both refusals. The customer asked for no time
        # at all - "snooze for zero hours" is not a request worth honouring -
        # or this severity's configured cap is 0, meaning it cannot be snoozed
        # (decision 8's option 3). Either way, returning None says so, rather
        # than writing a row that expired the instant it was created. An
        # already-expired snooze is harmless today and reads as a mute the
        # moment some comparison is written the other way round.
        logger.info("snooze refused: %s on account %d, %s hours at %s",
                    rule, account_id, hours, severity or "unknown severity")
        return None
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=hours)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_snoozes (account_id, dedup_key, rule, requested_by, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (account_id, dedup_key, rule, requested_by, now.isoformat(), expires_at),
        )
        snooze_id = cur.fetchone()["id"]
    logger.info(
        "snooze: %s (%s) on account %d silenced by %s until %s",
        rule, severity or "unknown severity", account_id, requested_by, expires_at,
    )
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_snoozes WHERE id = %s", (snooze_id,))
        return cur.fetchone()


def active_snooze(db: Database, account_id: int, dedup_key: str) -> str | None:
    """The expiry of a live snooze on this alert, or None.

    Compared against `now` in the query rather than in Python so that a row
    whose expiry has passed can never be treated as live by a caller that
    forgot to check - there is only one place this comparison exists."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM guardian_snoozes WHERE account_id = %s AND dedup_key = %s "
            "AND expires_at > %s ORDER BY expires_at DESC LIMIT 1",
            (account_id, dedup_key, datetime.now(timezone.utc).isoformat()),
        )
        row = cur.fetchone()
    return row["expires_at"] if row else None


def active_snoozes_for_accounts(db: Database, account_ids: list[int]) -> dict[str, str]:
    """dedup_key -> expiry, for rendering. One query rather than one per row."""
    if not account_ids:
        return {}
    marks = ",".join("%s" for _ in account_ids)
    with db.cursor() as cur:
        cur.execute(
            f"SELECT dedup_key, MAX(expires_at) AS expires_at FROM guardian_snoozes "
            f"WHERE account_id IN ({marks}) AND expires_at > %s GROUP BY dedup_key",
            [*account_ids, datetime.now(timezone.utc).isoformat()],
        )
        return {r["dedup_key"]: r["expires_at"] for r in cur.fetchall()}
