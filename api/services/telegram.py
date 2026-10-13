"""Telegram ops alerts (Bot API ``sendMessage``). Additional to the email
digest and Slack, never a replacement for either.

Guarantees, in order of importance:

* **Never raises.** Every public coroutine is wrapped; a failure of any kind
  (network, timeout, Telegram rejecting the message, a bug in a formatter)
  logs one WARNING and returns False. Nothing here can reach the executor,
  block a purchase or change a transaction's outcome, and every call site
  runs after the database write.
* **No-op when unset.** Without TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID the
  app runs exactly as before: no request is made, nothing is logged above
  DEBUG.
* **The token never appears in a log line.** Our own messages never include
  the request URL, and a redaction filter on the httpx/httpcore loggers scrubs
  ``bot<id>:<token>`` from theirs (httpx logs every request URL at INFO).
* **5 s timeout, one retry** on a transport error (connection refused,
  timeout); never a retry on an HTTP 4xx/5xx answer.
* **Rate limit:** at most TELEGRAM_MAX_PER_MINUTE messages per minute
  (in-process sliding window; the scheduler runs in the one API process).
  Excess messages are dropped, counted and logged.
* **Burst collapse:** a message whose collapse key matches one sent within
  COLLAPSE_WINDOW_S is suppressed and counted; the count goes out as one
  summary line on :func:`flush` (the scheduler calls it after every run) or
  rides on the next message with that key. Failures are keyed by their
  reason, so a retry storm of the same error is one message plus a count.
* **No secrets in messages:** never an API key, secret, token, memo/tag, full
  wallet address or full email. Emails show two characters plus the domain;
  addresses show the first 6 and last 4 characters.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Tuple

import httpx

from api.config import settings

logger = logging.getLogger("autobuy.telegram")

API_BASE = "https://api.telegram.org"
TIMEOUT_S = 5.0
COLLAPSE_WINDOW_S = 60.0
MAX_TEXT = 4000          # Telegram's limit is 4096 characters

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{10,}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{26,}")   # wallet addresses, keys, ids, hashes inside an error string


class _RedactToken(logging.Filter):
    """Scrubs the bot token from any log record of the HTTP client libraries."""
    def filter(self, record: logging.LogRecord) -> bool:   # noqa: D401
        try:
            if record.args:
                record.msg = record.getMessage()
                record.args = ()
            record.msg = _TOKEN_RE.sub("bot<redacted>", str(record.msg))
        except Exception:   # a logging filter must never break logging
            pass
        return True


for _name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection", "httpcore.proxy"):
    logging.getLogger(_name).addFilter(_RedactToken())


# ------------------------------------------------------------------ masking

def mask_email(email: Optional[str]) -> str:
    if not email or "@" not in email:
        return "unknown"
    local, domain = email.split("@", 1)
    return f"{local[:2]}…@{domain}"


def mask_address(address: Optional[str]) -> str:
    if not address:
        return "—"
    a = str(address).strip()
    if len(a) <= 12:
        return f"{a[:3]}…"
    return f"{a[:6]}…{a[-4:]}"


def scrub(text: Optional[str], limit: int = 160) -> str:
    """An error reason fit for a phone: emails and long alphanumeric runs
    (addresses, keys, ids) masked, then truncated."""
    s = str(text or "").strip() or "no reason given"
    s = _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), s)
    s = _LONG_TOKEN_RE.sub(lambda m: mask_address(m.group(0)), s)
    return s[:limit]


def _price(value) -> str:
    """At least 4 significant digits with thousands separators (0.000004181, 84,487.36)."""
    from api.services.history import fmt_price
    try:
        return fmt_price(float(value))
    except (TypeError, ValueError):
        return "—"


def _num(value, digits: int = 8) -> str:
    from api.services.history import fmt_amount
    try:
        return fmt_amount(float(value), digits)
    except (TypeError, ValueError):
        return "—"


# ------------------------------------------------------------------ state

@dataclass
class SendResult:
    ok: bool
    error: Optional[str] = None     # human text, never the token or the URL
    suppressed: bool = False        # dropped by the rate limit or collapsed into a count


@dataclass
class _Dup:
    first: float        # when the last message with this key was sent
    head: str           # its first line, for the summary
    count: int = 0      # identical messages suppressed since


_sent_at: Deque[float] = deque()
_dups: Dict[str, _Dup] = {}
dropped_total = 0
clock: Callable[[], float] = time.monotonic       # tests replace this
transport_factory: Callable[[], Optional[httpx.AsyncBaseTransport]] = lambda: None   # tests inject a MockTransport


def reset_state() -> None:
    global dropped_total
    _sent_at.clear()
    _dups.clear()
    dropped_total = 0


def configured() -> bool:
    token = (settings.telegram_bot_token or "").strip()
    chat = (settings.telegram_chat_id or "").strip()
    return bool(token and chat and not token.lower().startswith("your_"))


def _limit() -> int:
    try:
        return max(1, int(settings.telegram_max_per_minute))
    except (TypeError, ValueError):
        return 20


# ------------------------------------------------------------------ sending

async def _post(text: str, chat_id: Optional[str] = None) -> SendResult:
    """One sendMessage call with a single retry on a transport error.
    `chat_id` overrides TELEGRAM_CHAT_ID (the Guardian team group); the bot token is always the one."""
    url = f"{API_BASE}/bot{(settings.telegram_bot_token or '').strip()}/sendMessage"
    body = {"chat_id": (chat_id or settings.telegram_chat_id or "").strip(), "text": text[:MAX_TEXT],
            "disable_web_page_preview": True}
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport_factory()) as client:
                resp = await client.post(url, json=body)
        except httpx.TransportError as exc:      # timeouts, refused connections, DNS
            if attempt == 1:
                continue
            err = f"network error: {type(exc).__name__}"
            logger.warning("telegram: send failed after one retry (%s)", err)
            return SendResult(False, err)
        if resp.status_code < 300:
            return SendResult(True)
        description = None
        try:
            description = resp.json().get("description")
        except Exception:
            pass
        err = f"HTTP {resp.status_code}: {description or 'no description'}"[:200]
        logger.warning("telegram: sendMessage rejected (%s)", err)
        return SendResult(False, err)
    return SendResult(False, "unreachable")   # pragma: no cover


async def send_detailed(text: str, key: Optional[str] = None, *, force: bool = False,
                        chat_id: Optional[str] = None) -> SendResult:
    """Send one message. ``key`` enables burst collapsing; ``force`` bypasses
    collapsing (not the rate limit); ``chat_id`` sends to another chat of the
    same bot (the Guardian team group). Never raises."""
    global dropped_total
    try:
        if not configured():
            logger.debug("telegram skipped (not configured)")
            return SendResult(False, "not configured", suppressed=True)
        now = clock()
        if key is not None and not force:
            dup = _dups.get(key)
            if dup is not None and now - dup.first < COLLAPSE_WINDOW_S:
                dup.count += 1
                logger.debug("telegram: collapsed duplicate %r (%d so far)", key, dup.count)
                return SendResult(False, None, suppressed=True)
            if dup is not None and dup.count:
                text = f"{text}\n(+{dup.count} identical in the previous minute)"
            _dups[key] = _Dup(first=now, head=text.splitlines()[0] if text else key)
        while _sent_at and now - _sent_at[0] >= 60.0:
            _sent_at.popleft()
        if len(_sent_at) >= _limit():
            dropped_total += 1
            logger.warning("telegram: rate limit of %d/min reached; message dropped (%d dropped so far)", _limit(), dropped_total)
            return SendResult(False, "rate limited", suppressed=True)
        result = await _post(text, chat_id)
        if result.ok:
            _sent_at.append(now)
        return result
    except Exception as exc:   # a formatter or client bug must not reach the caller
        logger.warning("telegram: send failed: %s", type(exc).__name__)
        return SendResult(False, f"error: {type(exc).__name__}")


async def send(text: str, key: Optional[str] = None, *, chat_id: Optional[str] = None) -> bool:
    return (await send_detailed(text, key, chat_id=chat_id)).ok


def send_sync(text: str, key: Optional[str] = None, *, chat_id: Optional[str] = None) -> SendResult:
    """`send_detailed` for a thread without an event loop: the Guardian worker
    (v0.9.1). The worker is one process, so the in-process rate limit and
    burst collapse are correct there. Never raises."""
    import asyncio
    try:
        return asyncio.run(send_detailed(text, key, chat_id=chat_id))
    except Exception as exc:   # a loop or client problem must not reach the caller
        logger.warning("telegram: sync send failed: %s", type(exc).__name__)
        return SendResult(False, f"error: {type(exc).__name__}")


async def flush() -> int:
    """Send one summary line per collapse key that suppressed messages, and
    reset the counts. The scheduler calls this at the end of every run so a
    storm within a run is reported once, with its count. Never raises."""
    sent = 0
    try:
        if not configured():
            _dups.clear()
            return 0
        for key, dup in list(_dups.items()):
            if dup.count <= 0:
                continue
            n = dup.count
            dup.count = 0
            if (await send_detailed(f"{dup.head}\n↳ repeated {n} more time{'s' if n != 1 else ''} in this run", None)).ok:
                sent += 1
    except Exception as exc:
        logger.warning("telegram: flush failed: %s", type(exc).__name__)
    return sent


# ------------------------------------------------------------------ messages

def format_purchase_event(event) -> Optional[Tuple[str, str]]:
    """(text, collapse key) for a purchase event, or None for events that do
    not go to Telegram. Pure: no I/O, no secrets."""
    from api.services.history import presented_status
    p, tx = event.purchase, event.tx
    coin = p.crypto_symbol
    who = mask_email(getattr(event.user, "email", None))
    dry = "[dry run] " if event.dry_run else ""
    reason = scrub(event.error)
    if event.type == "buy_executed":
        cost = tx.cost_usd if tx.cost_usd is not None else p.amount_usd
        dest = "→ withdrawing to the wallet next" if p.purchase_type == "autobuy" else "held on the exchange"
        text = (f"{dry}✅ Purchase completed\n"
                f"{coin} · {float(cost):.2f} USDT → {_num(tx.amount_executed)} {coin} @ {_price(tx.fill_price)} USDT\n"
                f"{who} · {dest}")
        return text, f"buy_executed:{tx.id}"
    if event.type == "buy_failed":
        extra = event.extra or {}
        head = "⛔ Purchase stopped" if extra.get("stopped") else "❌ Purchase failed"
        tail = "\nStopped after repeated failures — needs a human." if extra.get("stopped") else ""
        text = (f"{dry}{head}\n{coin} · {float(p.amount_usd):.2f} USDT · attempt {event.attempt}/{event.max_attempts}\n"
                f"{who}\nReason: {reason}{tail}")
        return text, f"buy_failed:{'stopped:' if extra.get('stopped') else ''}{reason}"
    if event.type == "buy_needs_review":
        text = (f"{dry}🔍 Purchase parked for review\n{coin} · {float(p.amount_usd):.2f} USDT\n{who}\n"
                f"Reason: {reason}\nNeeds admin review (Admin → Review); not retried automatically.")
        return text, f"needs_review:{reason}"
    if event.type == "withdrawal_sent":
        simulated = presented_status(tx) == "simulated"
        text = (f"{dry}📤 Withdrawal sent{' (simulated, nothing left the exchange)' if simulated else ''}\n"
                f"{_num(tx.amount_executed)} {coin} → {mask_address(p.wallet_address)} ({tx.withdrawal_chain or p.blockchain_network})\n"
                f"{who} · id {tx.withdrawal_id or '—'}")
        return text, f"withdrawal_sent:{tx.id}"
    if event.type == "withdrawal_failed":
        text = (f"{dry}❌ Withdrawal failed\n{_num(tx.amount_executed)} {coin} → {mask_address(p.wallet_address)} "
                f"({tx.withdrawal_chain or p.blockchain_network})\n{who}\nReason: {reason}\n"
                f"Coins are still on the exchange; the withdrawal can be retried from Purchases.")
        return text, f"withdrawal_failed:{reason}"
    if event.type == "withdrawal_needs_info":
        text = (f"{dry}⚠️ Withdrawal needs information\n{_num(tx.amount_executed)} {coin} → {mask_address(p.wallet_address)}\n"
                f"{who}\nReason: {reason}\nThe exchange wants more details before it will send.")
        return text, f"withdrawal_needs_info:{reason}"
    return None


async def on_purchase_event(event) -> bool:
    """purchase_events subscriber. Never raises."""
    try:
        formatted = format_purchase_event(event)
        if formatted is None:
            return False
        text, key = formatted
        return await send(text, key)
    except Exception as exc:
        logger.warning("telegram: purchase event %s not sent: %s", getattr(event, "type", "?"), type(exc).__name__)
        return False


async def notify_withdrawal_confirmed(purchase, wd, dry_run: bool) -> bool:
    """Called by the withdrawal poller after the confirming commit. Never raises."""
    try:
        who = mask_email(getattr(getattr(purchase, "user", None), "email", None))
        text = (f"{'[dry run] ' if dry_run else ''}✅ Withdrawal confirmed\n"
                f"{_num(wd.amount_executed)} {purchase.crypto_symbol} → {mask_address(purchase.wallet_address)} "
                f"({wd.withdrawal_chain or purchase.blockchain_network})\n{who} · tx {wd.tx_hash or '—'}")
        return await send(text, f"withdrawal_confirmed:{wd.id}")
    except Exception as exc:
        logger.warning("telegram: withdrawal confirmation not sent: %s", type(exc).__name__)
        return False
