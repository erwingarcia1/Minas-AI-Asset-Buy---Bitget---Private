"""Customers telling us an alert was their own activity (G-24, #28).

Three things this is, and one thing it deliberately is not.

**It is a judgement about an alert, recorded as one.** A marked alert is a
false positive stated by the only person who can know - the account's owner.
That is the single most valuable signal this product can collect about whether
its detectors are any good, and until now there was nowhere to put it.

**It is a pattern, not a fact.** One customer marking one withdrawal is noise.
The same customer marking `never_touched_pair_activity` eleven times is a
statement about the rule: it is firing on how they trade. So every marking is
kept, and `patterns` is what an operator reads.

**It is honest about the denominator.** Marking gives us confirmed false
positives and nothing else - an unmarked alert has not been judged, it has
merely not been marked, and most alerts are never judged by anybody. So the
numbers here are reported as "of the alerts a customer judged", never as a
precision figure over all alerts. Dividing by the total would let a quiet week
of unjudged alerts read as a detector getting better, which is the same
"absence rendered as reassurance" this project keeps finding.

**It does not suppress anything.** Marking an alert does not silence the rule,
the account, or the next alert - `snoozes` does that, it is bounded, and it
expires. A reassuring click that quietly turned a detector off would be the
worst kind of feature in a product whose entire job is to keep watching.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from guardian.db import Database

logger = logging.getLogger("guardian.expected")

# A note is a customer's own words, stored and shown back to operators. Bounded
# because it arrives from a POST body, which is whatever the sender types.
MAX_NOTE_LENGTH = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_expected(db: Database, alert_id: int, account_id: int, rule: str,
                  dedup_key: str, marked_by: str, note: str | None = None) -> bool:
    """Record that this alert was the customer's own activity. False if they
    had already marked it.

    Does NOT check that the alert belongs to the caller - the caller does, in
    the same statement that finds the alert. See `portal._member_alert`: the
    portal looks the alert up within the member's accounts and passes what it
    found, so an alert id from another customer never reaches here."""
    normalized = (marked_by or "").strip().lower()
    if not normalized:
        # No anonymous markings. "Somebody said this was expected" is not a
        # fact an operator can act on, and this table exists to be acted on.
        raise ValueError("a marking must name who made it")
    clean_note = (note or "").strip()[:MAX_NOTE_LENGTH] or None
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_expected_activity "
            "(alert_id, account_id, rule, dedup_key, note, marked_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (alert_id, marked_by) DO NOTHING",
            (alert_id, account_id, rule, dedup_key, clean_note, normalized, _now()),
        )
        if cur.rowcount != 1:
            return False
    logger.info("alert %d marked as expected activity by %s (rule %s)",
                alert_id, normalized, rule)
    return True


def marked_alert_ids(db: Database, account_ids: list[int]) -> set[int]:
    """Which of these accounts' alerts have been marked. Used to render the
    control as already-answered rather than offering it twice."""
    if not account_ids:
        return set()
    marks = ",".join("%s" for _ in account_ids)
    with db.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT alert_id FROM guardian_expected_activity WHERE account_id IN ({marks})",
            account_ids,
        )
        return {r["alert_id"] for r in cur.fetchall()}


def marks_for_alert(db: Database, alert_id: int) -> list:
    with db.cursor() as cur:
        cur.execute(
            "SELECT marked_by, note, created_at FROM guardian_expected_activity "
            "WHERE alert_id = %s ORDER BY created_at",
            (alert_id,),
        )
        return cur.fetchall()


def patterns(db: Database, limit: int = 20) -> list:
    """What customers keep marking, worst first (G-24's third criterion).

    Grouped by rule and account, because those are the two questions an
    operator has: is this rule wrong, or is this customer's trading unusual?
    A rule marked across many accounts is a rule to fix; a rule marked many
    times on one account is a threshold to tune for that customer (#17/G-13).

    `alerts_total` is the count of alerts of that rule on that account, so the
    ratio can be read WITH its denominator rather than as a bare rate. A rule
    marked 3 times out of 4 and one marked 3 times out of 900 are different
    findings, and a percentage alone hides which one you are looking at."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT e.rule, e.account_id, ac.label, COUNT(*) AS marks, "
            "MAX(e.created_at) AS last_marked_at, "
            "(SELECT COUNT(*) FROM guardian_alerts al WHERE al.rule = e.rule "
            " AND al.account_id = e.account_id) AS alerts_total "
            "FROM guardian_expected_activity e JOIN guardian_accounts ac ON ac.id = e.account_id "
            "GROUP BY e.rule, e.account_id, ac.label "
            "ORDER BY marks DESC, last_marked_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def judged_counts(db: Database, rule: str | None = None) -> dict:
    """Confirmed false positives, and the judged population they came from.

    Deliberately NOT a precision ratio over all alerts. `marked` is the number
    of alerts a customer judged and rejected; `judged` is the number anybody
    judged at all. An unmarked alert is unjudged, not correct, and treating the
    two as the same would turn silence into a good score - which is precisely
    the failure mode this codebase keeps finding elsewhere.

    An honest precision figure needs a confirmed-true-positive signal, which
    does not exist yet; when it does, it belongs in this function's numerator
    and this docstring should stop apologising."""
    params: list = []
    rule_clause = ""
    if rule:
        rule_clause = " WHERE rule = %s"
        params.append(rule)
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(DISTINCT alert_id) AS n FROM guardian_expected_activity{rule_clause}",
                    params)
        marked = cur.fetchone()["n"]
        # Judged = marked as expected, or acknowledged by somebody. An
        # acknowledgement is not a verdict on correctness, so it is counted as
        # "a human looked", never as "the alert was right".
        ack_clause = " WHERE rule = %s" if rule else ""
        cur.execute(
            "SELECT COUNT(*) AS n FROM guardian_alerts "
            f"WHERE acknowledged_at IS NOT NULL{ack_clause.replace(' WHERE', ' AND')}",
            params,
        )
        acknowledged = cur.fetchone()["n"]
        cur.execute(f"SELECT COUNT(*) AS n FROM guardian_alerts{rule_clause}", params)
        total = cur.fetchone()["n"]
    return {
        "marked_expected": marked,
        "acknowledged": acknowledged,
        "alerts_total": total,
    }
