"""Multi-channel notification dispatcher.

Routes an event to email / SMS / push according to the user's stored
preferences, honors quiet hours for interruptive channels (SMS + push), lets
critical events override quiet hours, and records every attempt in the
notifications audit table. Channel failures never propagate.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from api.models import Notification, User
from api.services.email_service import email_service
from api.services.push_service import push_service
from api.services.sms_service import sms_service

logger = logging.getLogger("autobuy.notify")

LARGE_TRANSACTION_USDT = 1000.0


@dataclass
class Preferences:
    email_enabled: bool = True
    sms_enabled: bool = False
    push_enabled: bool = True
    quiet_hours_start: int = 21   # 9pm, user-local expressed as UTC offset hours
    quiet_hours_end: int = 8      # 8am

    @classmethod
    def for_user(cls, user: User) -> "Preferences":
        if not user.notification_preferences:
            return cls()
        try:
            raw = json.loads(user.notification_preferences)
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except (ValueError, TypeError):
            logger.warning("bad notification_preferences for user %s", user.id)
            return cls()

    def to_json(self) -> str:
        return json.dumps(self.__dict__)


def in_quiet_hours(prefs: Preferences, now: datetime | None = None) -> bool:
    hour = (now or datetime.now(timezone.utc)).hour
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window wraps midnight


class NotificationDispatcher:
    async def notify(self, db, user: User, template: str, subject: str,
                     body: str, critical: bool = False,
                     html: str | None = None, include_email: bool = True) -> dict:
        """Fan an event out to the user's enabled channels. Returns
        {channel: status} for observability/testing. `include_email=False` is
        for events whose rich email already went out separately."""
        prefs = Preferences.for_user(user)
        quiet = in_quiet_hours(prefs) and not critical
        results = {}

        if not include_email:
            results["email"] = "skipped_pref"
        elif prefs.email_enabled:
            ok = await email_service._send(user.email, subject,
                                           html or f"<p>{body}</p>", body,
                                           template=template)
            results["email"] = "sent" if ok else "failed"
        else:
            results["email"] = "skipped_pref"

        if not prefs.sms_enabled or not user.phone_number:
            results["sms"] = "skipped_pref"
        elif quiet:
            results["sms"] = "skipped_quiet"
        else:
            ok = await sms_service.send_sms(user.phone_number, f"{subject} — {body}")
            results["sms"] = "sent" if ok else "failed"

        if not prefs.push_enabled or not user.device_token:
            results["push"] = "skipped_pref"
        elif quiet:
            results["push"] = "skipped_quiet"
        else:
            ok = await push_service.send_push(user.device_token, subject, body,
                                              {"template": template})
            results["push"] = "sent" if ok else "failed"

        try:
            for channel, status in results.items():
                db.add(Notification(user_id=user.id, channel=channel,
                                    template=template, subject=subject[:255],
                                    status=status))
            await db.commit()
        except Exception:
            logger.exception("notification audit write failed for user %s", user.id)
        return results


dispatcher = NotificationDispatcher()
