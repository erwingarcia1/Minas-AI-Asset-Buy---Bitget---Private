from __future__ import annotations

from guardian.detectors.base import Detection


def detect_unreachable(
    consecutive_failures: int, threshold: int, auth_failures: int = 0
) -> list[Detection]:
    """Account unreachable for N consecutive polls.

    HIGH rather than MEDIUM (G-6). MEDIUM routes to `digest` alone, so this -
    the alert that says a customer has stopped being monitored - waited for a
    daily digest sent by a host crontab that `docs/deployment.md` warns "fails
    silently every day". An account could go unwatched indefinitely with the
    customer's only signal being a digest that never arrived, which is silence
    looking exactly like safety.

    `auth_failures` splits the message, because the two cases need different
    things from the customer. A rejected key is theirs to re-issue; an exchange
    that won't answer is not, and telling them to go and fix it would be wrong.
    It does not change the severity: either way they are not being watched, and
    that is what this alert is about.

    Distinct from the tamper signal, which reads repeated *auth* failures as a
    possible attack. This one is about coverage, not attribution - both can be
    true at once and both should fire."""
    if consecutive_failures < threshold:
        return []

    if auth_failures >= threshold:
        message = (
            f"Your account is NOT being watched. The exchange has rejected our read-only "
            f"key {auth_failures} times in a row - it has most likely expired or been "
            f"removed. Re-issue a read-only API key on the exchange and send it to Minas "
            f"to restore monitoring."
        )
    else:
        message = (
            f"Your account is NOT being watched. We have been unable to reach the exchange "
            f"for {consecutive_failures} consecutive checks. Nothing is needed from you yet "
            f"- Minas is on it - but until it clears, movements on this account are not "
            f"being detected."
        )

    return [
        Detection(
            rule="account_unreachable",
            severity="HIGH",
            confidence="high",
            message=message,
            evidence={
                "consecutive_failures": consecutive_failures,
                "auth_failures": auth_failures,
                "threshold": threshold,
            },
        )
    ]


def detect_baseline_drift(new_deposit_addresses: list[str]) -> list[Detection]:
    """LOW-severity human-review signal: new but low-risk behavior. A new
    deposit sender address is normal account life, but a reviewer looking at
    a later incident wants these on the timeline."""
    if not new_deposit_addresses:
        return []
    return [
        Detection(
            rule="baseline_drift_new_deposit_source",
            severity="LOW",
            confidence="low",
            message=f"{len(new_deposit_addresses)} new deposit source address(es) added to baseline",
            evidence={"addresses": new_deposit_addresses},
        )
    ]
