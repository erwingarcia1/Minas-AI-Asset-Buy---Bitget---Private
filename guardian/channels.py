"""Which channel serves which severity, from rules.yaml and the platform settings.

Ported from Guardian's cli.build_channels / build_operator_channels. The
named channels are the platform's transports: `telegram` (TELEGRAM_CHAT_ID),
`telegram_team` (GUARDIAN_TELEGRAM_CHAT_ID), `sms` (ClickSend preferred,
Twilio fallback, to CLICKSEND_TO_NUMBERS or TWILIO_TO_NUMBERS) and `email`
(every active admin, through the Resend outbox). 'digest' entries are skipped
here: MEDIUM/LOW alerts are stored and delivered by the daily digest.
"""
from __future__ import annotations

import logging

from guardian.rules_schema import default_for

logger = logging.getLogger("guardian.channels")

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def admin_emails(db) -> list[str]:
    """Operator email goes to the platform's admins."""
    with db.cursor() as cur:
        cur.execute("SELECT email FROM users WHERE role = 'admin' AND NOT suspended ORDER BY email")
        return [row["email"] for row in cur.fetchall()]


def build_named_channels(db=None) -> dict[str, object]:
    """Instantiates every channel that has configuration; the routing in
    rules.yaml decides which severities use which."""
    from api.config import settings
    from guardian.alerts.email import OutboxEmailChannel, email_configured
    from guardian.alerts.sms import build_sms_channel
    from guardian.alerts.telegram import TelegramChannel

    named: dict[str, object] = {}
    token = (settings.telegram_bot_token or "").strip()
    if token and (settings.telegram_chat_id or "").strip():
        named["telegram"] = TelegramChannel()
    team = (settings.guardian_telegram_chat_id or "").strip()
    if token and team:
        named["telegram_team"] = TelegramChannel(team)
    sms_to = (settings.clicksend_to_numbers or settings.twilio_to_numbers or "").strip()
    if sms_to:
        channel = build_sms_channel([n.strip() for n in sms_to.split(",") if n.strip()])
        if channel is not None:
            named["sms"] = channel
    if email_configured() and db is not None:
        recipients = admin_emails(db)
        if recipients:
            named["email"] = OutboxEmailChannel(recipients, audience="operator")
    return named


def build_channels(rules, problems: list | None = None, *, db=None) -> dict[str, list]:
    """severity -> [channel objects], from alerting.routing in rules.yaml."""
    named = build_named_channels(db)
    channels: dict[str, list] = {}
    for severity in SEVERITIES:
        routed = rules.get("alerting", "routing", severity, default=None)
        if not routed:
            routed = default_for("alerting", "routing", severity) or []
            logger.error("no usable alert routing for %s - falling back to %s. An alert with no "
                         "channel is an alert that is never delivered.", severity, ", ".join(routed))
            if problems is not None:
                problems.append({"kind": "fallback", "channel": None, "severity": severity})
        resolved = []
        for name in routed:
            if name == "digest":
                continue
            channel = named.get(str(name))
            if channel is None:
                logger.warning("channel '%s' routed for %s but not configured", name, severity)
                if problems is not None:
                    problems.append({"kind": "unconfigured", "channel": str(name), "severity": severity})
                continue
            resolved.append(channel)
        channels[severity] = resolved
    return channels


def build_operator_channels(rules, *, db=None) -> dict[str, list]:
    """severity -> [channel objects], from `operator_alerting.routing` (G-49):
    the same named channels, resolved through a separate table so paging
    ourselves about an outage can't be changed by retuning a customer's
    alert preferences. 'digest' is meaningless for a page and is skipped."""
    named = build_named_channels(db)
    channels: dict[str, list] = {}
    for severity in SEVERITIES:
        routed = rules.get("operator_alerting", "routing", severity, default=None)
        if not routed:
            routed = default_for("operator_alerting", "routing", severity) or []
            logger.error("no usable operator routing for %s - falling back to %s", severity, ", ".join(routed))
        resolved = []
        for name in routed:
            if name == "digest":
                continue
            channel = named.get(str(name))
            if channel is None:
                logger.warning("operator channel '%s' routed for %s but not configured", name, severity)
                continue
            resolved.append(channel)
        channels[severity] = resolved
    return channels


def digest_channels(db=None) -> list:
    """Where the daily MEDIUM/LOW digest goes: Telegram and the admins' email."""
    named = build_named_channels(db)
    return [c for name, c in named.items() if name in ("telegram", "email")]
