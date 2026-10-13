from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from guardian.alerts.base import AlertMessage
from guardian.alerts.email import OutboxEmailChannel, email_configured
from guardian.alerts.sms import build_sms_channel

logger = logging.getLogger("guardian.alerts.contact")


class ContactRouter:
    """Delivers an account's alerts to the per-account email/phone captured at
    onboarding, in addition to the global severity routing. Email goes through
    the platform's Resend outbox and SMS through ClickSend / Twilio from the
    platform settings; when a transport isn't configured, a contact is skipped
    with a one-time warning rather than an error - so per-account delivery
    lights up the moment credentials are added, with no code change.

    When constructed with `db` (and optionally `rules`), SMS delivery is also
    policy-aware and auditable:
      - every SMS outcome lands in sms_deliveries (provider message id on
        success, provider error on failure, skip/suppress reasons otherwise);
      - duplicate SMS for the same rule + account are suppressed within
        alerting.sms_cooldown_minutes (default 30);
      - the account's sms_alerts_enabled preference is respected, with
        CRITICAL overriding an opt-out when
        alerting.sms_critical_overrides_optout is true (default);
      - a CRITICAL alert with no phone on file is logged and recorded so the
        dashboard can nudge the operator to capture one."""

    def __init__(self, db=None, rules=None, operator_alerter=None) -> None:
        self._db = db
        self._rules = rules
        self._email_ok = email_configured()
        self._warned: set[str] = set()
        self._operator_alerter = operator_alerter

    def _warn_once(self, key: str, msg: str) -> None:
        """A missing provider is not a transient (#58, group A).

        The per-event failures on this path - one email bounced, one SMS
        rejected - are deliberately left as log lines: one is noise, and a
        rising rate is a metric-and-alarm shape rather than a page. This is the
        other kind. A contact channel that is *set on the account* but has no
        provider configured means every alert that account should ever receive
        through it silently does not happen, for the life of the process. It is
        permanent, it is a configuration mistake somebody can fix in a minute,
        and until now it was one log line at the moment of the first
        undelivered alert.

        Paged once per process, on the same key that suppresses the log."""
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(msg)
        if self._operator_alerter is None:
            return
        self._operator_alerter.page(
            rule="contact_channel_has_no_provider",
            severity="HIGH",
            message=(
                f"A customer contact channel is configured on at least one account but has no "
                f"provider set, so those alerts are not being delivered: {msg}"
            ),
            evidence={"channel": key},
            dedup_scope=key,
        )

    # --- SMS delivery log ----------------------------------------------------

    def _record_sms(
        self,
        account_id: int | None,
        alert_id: int | None,
        rule: str,
        status: str,
        *,
        phone: str | None = None,
        provider: str | None = None,
        provider_message_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        if self._db is None or account_id is None:
            return
        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO guardian_sms_deliveries
                    (account_id, alert_id, rule, phone, provider, provider_message_id, status, detail, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_id,
                    alert_id,
                    rule,
                    phone,
                    provider,
                    provider_message_id,
                    status,
                    detail,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def _sms_pref_enabled(self, account_id: int) -> bool:
        with self._db.cursor() as cur:
            cur.execute("SELECT sms_alerts_enabled FROM guardian_accounts WHERE id = %s", (account_id,))
            row = cur.fetchone()
        return bool(row["sms_alerts_enabled"]) if row is not None else True

    def _sms_suppressed(self, account_id: int, rule: str) -> bool:
        """Duplicate-SMS protection: has an SMS for this rule + account already
        gone out within the suppression window? Keyed wider than dispatch's
        dedup (rule, not rule + evidence id) - a burst of distinct withdrawals
        still yields one text, with the details on Telegram/email."""
        minutes = 30
        if self._rules is not None:
            minutes = self._rules.get_int("alerting", "sms_cooldown_minutes", default=30)
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT created_at FROM guardian_sms_deliveries WHERE account_id = %s AND rule = %s "
                "AND status = 'sent' ORDER BY created_at DESC LIMIT 1",
                (account_id, rule),
            )
            row = cur.fetchone()
        if row is None:
            return False
        last = datetime.fromisoformat(row["created_at"])
        return datetime.now(timezone.utc) - last < timedelta(minutes=minutes)

    def _critical_overrides_optout(self) -> bool:
        if self._rules is None:
            return True
        return bool(self._rules.get("alerting", "sms_critical_overrides_optout", default=True))

    # --- delivery --------------------------------------------------------------

    def send(self, detection, account_label: str, emails, phones, *, account_id=None, alert_id=None) -> None:
        """Deliver to every configured per-account email + phone (primary and
        secondary). `emails`/`phones` are iterables; None/blank entries and
        duplicates are dropped. Telegram handles are intentionally NOT handled
        here - the Bot API can't DM an arbitrary @handle without the recipient
        first starting the bot (see docs), so those are stored for reference
        only until a customer-opt-in flow exists."""
        emails = list(dict.fromkeys(e for e in (emails or []) if e))
        phones = list(dict.fromkeys(p for p in (phones or []) if p))
        auditable = self._db is not None and account_id is not None

        # A CRITICAL alert that has nowhere to text is itself worth surfacing:
        # log it and record it so the dashboard can prompt for a phone number.
        if not phones and detection.severity == "CRITICAL" and auditable:
            logger.warning(
                "CRITICAL alert for %s has no SMS destination on file - add a phone number "
                "to enable text alerts (rule=%s)",
                account_label, detection.rule,
            )
            self._record_sms(
                account_id, alert_id, detection.rule, "skipped",
                detail="no phone number on file",
            )

        if not emails and not phones:
            return
        alert = AlertMessage(
            account_label=account_label,
            rule=detection.rule,
            severity=detection.severity,
            confidence=detection.confidence,
            message=detection.message,
            evidence=detection.evidence,
            # Account notices (password reset, destination change) carry their
            # whole meaning in `message` and must render it verbatim; a
            # detection must not, because that text embeds addresses.
            notice=getattr(detection, "is_notice", False),
        )

        if emails:
            if not self._email_ok:
                self._warn_once(
                    "email",
                    "a customer has an email destination but email is not configured - set "
                    "RESEND_API_KEY and RESEND_FROM_EMAIL to deliver these alerts (Telegram still fires)",
                )
            else:
                try:
                    OutboxEmailChannel(emails, audience="customer").send(alert)
                except Exception as exc:  # noqa: BLE001 - delivery failure must not stall detection
                    # Stays a log line, decided in the #58 sweep. One bounced
                    # email is noise - a wrong address, a full mailbox, a
                    # greylisting retry. A *rising rate* is a broken SMTP
                    # config, and that is metric-and-alarm shaped, not
                    # page-shaped. Do not re-litigate this without the metric.
                    logger.warning("per-account email to %s failed: %s", emails, exc)

        if phones:
            self._send_sms(detection, alert, account_label, phones, account_id, alert_id, auditable)

    def _send_sms(self, detection, alert, account_label, phones, account_id, alert_id, auditable) -> None:
        # Member preference: opted-out accounts get no SMS, except CRITICAL
        # when the business rule says a critical alert must still page.
        if auditable and not self._sms_pref_enabled(account_id) and (
            detection.severity != "CRITICAL" or not self._critical_overrides_optout()
        ):
                self._record_sms(
                    account_id, alert_id, detection.rule, "skipped",
                    detail="sms_alerts disabled by member preference",
                )
                return

        # Duplicate-SMS protection (same rule + account within the window).
        if auditable and self._sms_suppressed(account_id, detection.rule):
            self._record_sms(
                account_id, alert_id, detection.rule, "suppressed",
                detail="duplicate SMS suppressed within cooldown window",
            )
            return

        channel = build_sms_channel(phones)
        if channel is None:
            self._warn_once(
                "sms",
                "per-account SMS set but no SMS provider is configured - set CLICKSEND_* or "
                "TWILIO_* env to deliver these alerts (Telegram still fires)",
            )
            return
        try:
            receipts = channel.send(alert)
        except Exception as exc:  # noqa: BLE001 - delivery failure must not stall detection
            # Stays a log line for the same reason as the email path above
            # (#58), and this one additionally leaves an `sms_deliveries` row
            # with status='failed' - a queryable record, which is more than the
            # email path has.
            logger.warning("per-account SMS to %s failed: %s", phones, exc)
            for phone in phones:
                self._record_sms(
                    account_id, alert_id, detection.rule, "failed",
                    phone=phone, provider=type(channel).__name__, detail=str(exc),
                )
            return
        # Channels return one receipt per number; a stub/legacy channel that
        # returns None still gets per-phone 'sent' rows (no provider id).
        if receipts is None:
            receipts = [{"to": p, "provider": None, "provider_message_id": None} for p in phones]
        for receipt in receipts:
            self._record_sms(
                account_id, alert_id, detection.rule, "sent",
                phone=receipt.get("to"),
                provider=receipt.get("provider"),
                provider_message_id=receipt.get("provider_message_id"),
            )
