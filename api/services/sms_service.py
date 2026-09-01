"""SMS via the Twilio REST API. Unconfigured (or placeholder) credentials make
every send a logged no-op — same contract as the email service."""
import logging

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.sms")


class SMSService:
    def __init__(self, sid: str = "", token: str = "", from_number: str = ""):
        self.sid = sid or settings.twilio_account_sid
        self.token = token or settings.twilio_auth_token
        self.from_number = from_number or settings.twilio_from_number

    @property
    def configured(self) -> bool:
        return bool(self.sid and self.token and self.from_number
                    and not self.sid.startswith("your_"))

    async def send_sms(self, phone_number: str, message: str) -> bool:
        if not self.configured:
            logger.info("sms skipped (Twilio not configured): to=%s", phone_number)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                    auth=(self.sid, self.token),
                    data={"From": self.from_number, "To": phone_number,
                          "Body": message[:1600]})
            ok = resp.status_code < 400
            (logger.info if ok else logger.error)(
                "sms %s: to=%s status=%s", "sent" if ok else "failed",
                phone_number, resp.status_code)
            return ok
        except Exception:
            logger.exception("sms send raised: to=%s", phone_number)
            return False


sms_service = SMSService()
