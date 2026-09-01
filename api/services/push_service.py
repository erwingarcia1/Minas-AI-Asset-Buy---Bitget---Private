"""Push notifications via Firebase Cloud Messaging. Unconfigured server key
makes every send a logged no-op."""
import logging

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.push")

FCM_URL = "https://fcm.googleapis.com/fcm/send"


class PushService:
    def __init__(self, server_key: str = ""):
        self.server_key = server_key or settings.fcm_server_key

    @property
    def configured(self) -> bool:
        return bool(self.server_key and not self.server_key.startswith("your_"))

    async def send_push(self, device_token: str, title: str, body: str,
                        data: dict | None = None) -> bool:
        if not self.configured:
            logger.info("push skipped (FCM not configured): title=%r", title)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    FCM_URL,
                    headers={"Authorization": f"key={self.server_key}"},
                    json={"to": device_token,
                          "notification": {"title": title, "body": body},
                          "data": data or {}})
            ok = resp.status_code < 400
            (logger.info if ok else logger.error)(
                "push %s: title=%r status=%s", "sent" if ok else "failed",
                title, resp.status_code)
            return ok
        except Exception:
            logger.exception("push send raised: title=%r", title)
            return False


push_service = PushService()
