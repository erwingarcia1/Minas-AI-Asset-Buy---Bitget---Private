"""A customer replacing their own monitoring key (#69, G-1/G-3 adjacent).

The runbook printed on every CRITICAL and HIGH alert tells a frightened person
to revoke API keys, and names ours as the one to leave alone - because it is
read-only, so revoking it takes nothing from an attacker and switches the
monitoring off. Panic being what it is, some customers will revoke everything
anyway. Until this module existed, the recovery path was "ask an operator",
which at 3am on a Sunday means an active incident continues unobserved for
hours.

**The attack this is written against.** Accepting a new key from a signed-in
member sounds like a small thing: validate it is read-only (G-4) and store it.
But a key can be genuinely read-only, pass every check, and belong to a
*different exchange account* - the submitter's own empty one. Store that and
the Guardian polls a quiet account, reports "watching", and the account the
customer believes is protected goes unobserved. That is the exact silent
failure this product exists to prevent, and it would be introduced by the
feature meant to help.

So a replacement must prove it is for the same account, by matching the
exchange's own account id recorded from an earlier account-info payload
(`accounts.exchange_uid`, learned at onboarding and refreshed by every slow
loop). No recorded id means no way to prove it, and the answer to "we cannot
tell" here is the same as everywhere else in this codebase: refuse, and route
the customer to a person.

Everything in this module refuses by default. There is no path where an
exception, a missing field or an unexpected response results in a stored key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from guardian.crypto import CredentialCipher
from guardian.db import Database
from guardian.events import append_event
from guardian.exchange import exchange_account_uid, make_client
from guardian.onboarding import verify_read_only

logger = logging.getLogger("guardian.keyreplacement")

# The event type that appears on the customer's own activity page (#36). Named
# in `events.ADMIN_EVENT_TYPES`; a replacement is administration, not a
# detection, and it is precisely the kind of change somebody checking "who has
# touched my account" is looking for.
REPLACED_EVENT = "monitoring_key_replaced"


@dataclass(frozen=True)
class ReplacementOutcome:
    """What happened, in two registers.

    `message` is read by a frightened customer and says what to do next.
    `detail` is for the log and the operator page. They are separate fields
    because the useful operator detail - which check failed, what the exchange
    answered - is exactly what should not be pasted in front of somebody who
    may be reading it over an attacker's shoulder."""

    ok: bool
    message: str
    detail: str = ""
    reason_code: str = ""


_CONTACT_US = ("Ask Minas for a new key - reply to any alert email, or use the "
               "contact details on your welcome message.")


def _refuse(reason_code: str, message: str, detail: str) -> ReplacementOutcome:
    return ReplacementOutcome(ok=False, message=message, detail=detail, reason_code=reason_code)


def replace_monitoring_key(
    db: Database,
    cipher: CredentialCipher,
    *,
    account_id: int,
    api_key: str,
    secret: str,
    passphrase: str = "",
    actor: str,
    operator_alerter=None,
) -> ReplacementOutcome:
    """Validate and install a replacement read-only key for one account.

    `actor` is the signed-in member's email. It is required, not optional:
    this is a person changing how their account is watched, and an unattributed
    credential change is the one thing the activity page must never contain.

    The caller is responsible for having checked that `actor` may act on
    `account_id` - `portal.member_accounts` is that boundary, and it is not
    re-derived here because a second, subtly different scoping rule is worse
    than one used consistently.
    """
    api_key = (api_key or "").strip()
    secret = (secret or "").strip()
    passphrase = (passphrase or "").strip()
    if not api_key or not secret:
        return _refuse(
            "incomplete",
            "Both the API key and its secret are needed. Nothing was changed.",
            "submitted form was missing the key or the secret",
        )

    with db.cursor() as cur:
        cur.execute("SELECT id, label, exchange, exchange_uid FROM guardian_accounts WHERE id = %s", (account_id,))
        account = cur.fetchone()
    if account is None:
        return _refuse("no_such_account", "That account could not be found.", "no account row")

    label = account["label"]
    exchange = account["exchange"]
    known_uid = (account["exchange_uid"] or "").strip()

    if not known_uid:
        # Fail closed, and page. A customer who gets here is stuck, and until
        # somebody is told, being stuck looks exactly like being served.
        _page(operator_alerter, account_id, label,
              rule="monitoring_key_replacement_needs_an_operator",
              severity="HIGH",
              message=(f"{label}: {actor} tried to replace the monitoring key, and we have no "
                       f"exchange account id on record to check the new key against, so it was "
                       f"refused. This account has not completed a slow loop since the id started "
                       f"being recorded, or its exchange does not report one. They need a key "
                       f"issuing by hand, and they are asking during an incident."),
              evidence={"account_label": label, "exchange": exchange, "requested_by": actor})
        return _refuse(
            "no_recorded_identity",
            "We cannot yet confirm that a new key belongs to this account, so we have not "
            "changed anything. Minas has been told you are waiting. " + _CONTACT_US,
            f"no exchange_uid recorded for account {account_id} ({exchange})",
        )

    try:
        client = make_client(exchange, api_key, secret, passphrase)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything at all
        logger.warning("account %s: could not build a client for a replacement key: %s", label, exc)
        return _refuse(
            "unusable",
            "That key could not be used to contact the exchange. Check it was copied in full "
            "and try again. Nothing was changed.",
            f"make_client raised: {exc}",
        )

    ok, reason = verify_read_only(client, exchange)
    if not ok:
        # The read-only guarantee is enforced, not promised (G-4). A key with
        # trade or withdrawal permission is refused here exactly as it is at
        # onboarding, and for the same reason: accepting one with a warning
        # makes the promise a preference.
        return _refuse(
            "not_read_only",
            "That key was refused because it is not read-only, or because we could not read "
            "the account with it. Create a new key with view/read permission only - no trading, "
            "no withdrawals - and try again. Nothing was changed.",
            f"verify_read_only refused: {reason}",
        )

    try:
        info = client.fetch_account_info()
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            "identity_unreadable",
            "We could not confirm which exchange account that key belongs to, so we have not "
            "changed anything. Please try again in a few minutes. " + _CONTACT_US,
            f"fetch_account_info raised after verify_read_only passed: {exc}",
        )

    candidate_uid = exchange_account_uid(exchange, info)
    if candidate_uid is None:
        return _refuse(
            "identity_unreadable",
            "We could not confirm which exchange account that key belongs to, so we have not "
            "changed anything. " + _CONTACT_US,
            f"{exchange} account-info payload carried no account id",
        )

    if candidate_uid != known_uid:
        # The case the whole module is shaped around. Worth a page whatever the
        # explanation: an honest customer pasting the wrong account's key
        # during an incident is someone about to believe they are protected,
        # and a dishonest one is an attacker trying to blind us.
        _page(operator_alerter, account_id, label,
              rule="monitoring_key_replacement_wrong_account",
              severity="HIGH",
              message=(f"{label}: {actor} submitted a valid read-only key belonging to a "
                       f"different exchange account. It was refused and nothing was changed. "
                       f"Either they are looking at the wrong account, or somebody is trying to "
                       f"point the monitoring somewhere harmless."),
              evidence={"account_label": label, "exchange": exchange, "requested_by": actor,
                        "expected_account": known_uid, "submitted_account": candidate_uid})
        return _refuse(
            "wrong_account",
            "That key is for a different exchange account, so we have not changed anything. "
            "Create the new key inside the same exchange account this Guardian watches, and "
            "check you are signed in to the right one.",
            f"submitted key belongs to {candidate_uid}, account is {known_uid}",
        )

    now = datetime.now(timezone.utc).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE guardian_accounts SET encrypted_api_key = %s, encrypted_secret = %s, "
            "encrypted_passphrase = %s, updated_at = %s WHERE id = %s",
            (cipher.encrypt(api_key), cipher.encrypt(secret), cipher.encrypt(passphrase), now, account_id),
        )

    # Attributed to the member, because a credential change with nobody's name
    # on it is the shape of an unauthorised one (G-32, G-50). The payload
    # carries no part of the key, not even a prefix: this row is readable in
    # the portal and kept for two years.
    append_event(
        db, account_id, REPLACED_EVENT, "HIGH",
        {"exchange": exchange, "replaced_by": actor},
        actor=actor,
    )

    # The auth-failure counters are deliberately not reset here. The next fast
    # loop clears them on its first success, which is the difference between
    # "a key was installed" and "a key works" - and only the second one is
    # worth telling a customer about.
    _page(operator_alerter, account_id, label,
          rule="monitoring_key_replaced",
          severity="MEDIUM",
          message=(f"{label}: {actor} replaced the monitoring key themselves. It validated as "
                   f"read-only and belongs to the expected exchange account. Monitoring resumes "
                   f"on the next poll. Unexpected key replacements are worth a look."),
          evidence={"account_label": label, "exchange": exchange, "replaced_by": actor})

    logger.info("account %s: monitoring key replaced by %s", label, actor)
    return ReplacementOutcome(
        ok=True,
        message=("Your new key is in place. Monitoring restarts within a minute, and you will "
                 "see this change on your activity page."),
        detail=f"replaced by {actor}",
        reason_code="replaced",
    )


def _page(alerter, account_id: int, label: str, **kwargs) -> None:
    """Page an operator, and never let paging failure change the outcome.

    A dead SMTP server must not turn a refused key into an accepted one, nor an
    accepted one into an error the customer retries - the decision has already
    been made and recorded by the time this runs."""
    if alerter is None:
        return
    try:
        alerter.page(account_id=account_id, dedup_scope=f"keyswap:{label}", **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("account %s: could not page an operator about a key replacement: %s", label, exc)
