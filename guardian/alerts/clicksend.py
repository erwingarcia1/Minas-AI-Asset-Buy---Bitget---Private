from __future__ import annotations

from guardian import net
from guardian.alerts.base import AlertChannel, AlertMessage
from guardian.alerts.sms import SMS_MAX_LEN, format_sms_body

# Verified against ClickSend's official docs + Python/PHP SDKs (2026-07-05):
# POST https://rest.clicksend.com/v3/sms/send, HTTP Basic auth (username + API
# key as password), body {"messages":[{"body","to","from?","source?"}]}, and
# success = response_code=="SUCCESS" AND each data.messages[].status=="SUCCESS".
_CLICKSEND_SMS_URL = "https://rest.clicksend.com/v3/sms/send"


class ClickSendSmsChannel(AlertChannel):
    """SMS via ClickSend's REST API - a Twilio alternative. SMS is a pager, not
    a report: the body is hard-truncated (full evidence goes out on Telegram/
    email in parallel)."""

    MAX_LEN = SMS_MAX_LEN

    def __init__(self, username: str, api_key: str, from_id: str | None, to_numbers: list[str], timeout: float = 15.0):
        self._username = username
        self._api_key = api_key
        self._from = from_id
        self._to = to_numbers
        self._timeout = timeout

    def send(self, alert: AlertMessage) -> list[dict]:
        """Sends to every recipient in one API call; returns one delivery
        receipt per number: {to, provider, provider_message_id, status}.
        Raises when ClickSend rejects the request or fails to queue any
        message, so callers can log the failure for admin review."""
        body = format_sms_body(alert, self.MAX_LEN)
        messages = []
        for number in self._to:
            msg = {"source": "minas", "body": body, "to": number}
            if self._from:
                msg["from"] = self._from
            messages.append(msg)

        resp = net.post(
            _CLICKSEND_SMS_URL,
            auth=(self._username, self._api_key),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"messages": messages},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # ClickSend docs: http_code/response_code alone don't reflect per-message
        # outcome. Require top-level SUCCESS AND every message queued.
        if data.get("response_code") != "SUCCESS":
            raise RuntimeError(f"ClickSend rejected the request: {data.get('response_code')} {data.get('response_msg')}")
        results = data.get("data", {}).get("messages", [])
        if not results:
            # SUCCESS with no per-message results is a malformed response, and
            # the per-message check below would pass it silently - recording an
            # SMS as delivered that the provider never acknowledged.
            raise RuntimeError("ClickSend returned SUCCESS with no message results")
        failed = [m for m in results if m.get("status") != "SUCCESS"]
        if failed:
            raise RuntimeError(
                f"ClickSend failed to queue {len(failed)} message(s): "
                f"{failed[0].get('status')} (to {failed[0].get('to')})"
            )
        # Response messages come back in request order; prefer ClickSend's own
        # `to` field, falling back to the requested number by index.
        receipts = []
        for i, number in enumerate(self._to):
            result = results[i] if i < len(results) else {}
            receipts.append(
                {
                    "to": result.get("to") or number,
                    "provider": "clicksend",
                    "provider_message_id": result.get("message_id"),
                    "status": "sent",
                }
            )
        return receipts
