"""Pre-buy low-balance warning (private beta, PR for v0.7.3).

A READ-ONLY scheduled job: about a day and about two hours before each active
purchase's next run it reads the available USDT in the purchase's exchange
sub-account and, when that is below the purchase amount plus a small margin,
warns the customer once per run (email, if the optional category is on, plus
the dashboard banner) and posts one masked line to the admin Telegram chat.
It never places an order, never touches the executor, and never changes a
purchase's schedule or status: the only writes are the two bookkeeping columns
from migration 026. An unreadable balance is reported to admins only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.config import settings
from api.models import APIKey, Purchase, User, utcnow
from api.services import telegram
from api.services.email_service import email_service
from api.services.email_templates import render
from api.services.notifications import Preferences

logger = logging.getLogger("autobuy.low_balance")

MARGIN = 0.01                       # 1% headroom over the purchase amount
JOB_INTERVAL_MINUTES = 30
# (earliest, latest) lead time before next_execution for each check window
WINDOW_ONE = (timedelta(hours=23), timedelta(hours=25))   # about a day before
WINDOW_TWO = (timedelta(hours=1), timedelta(hours=3))     # about two hours before
SUBJECT = "Top up before your next AutoBuy purchase"


def low_balance_outstanding(p: Purchase) -> bool:
    """True while the last warning is for the purchase's current next run (the banner shows)."""
    return (p.low_balance_alerted_for is not None and p.next_execution is not None
            and p.low_balance_alerted_for == p.next_execution)


def in_window(next_execution: datetime, now: datetime) -> Optional[int]:
    """1 or 2 when `now` falls in a check window for that run, else None. Past runs never match."""
    lead = next_execution - now
    if lead <= timedelta(0):
        return None
    if WINDOW_ONE[0] <= lead <= WINDOW_ONE[1]:
        return 1
    if WINDOW_TWO[0] <= lead <= WINDOW_TWO[1]:
        return 2
    return None


def needed_usdt(p: Purchase) -> float:
    return float(p.amount_usd) * (1.0 + MARGIN)


def read_usdt_balance(account: APIKey) -> float:
    """Available spot USDT on the account: one authenticated read, nothing else."""
    from api.services.exchange_gateway import _client_for
    return float(_client_for(account).get_account_balance("USDT"))


BalanceReader = Callable[[APIKey], float]


@dataclass
class Outcome:
    checked: int = 0
    alerted: int = 0
    already_alerted: int = 0
    cleared: int = 0
    unreadable: int = 0
    sufficient: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _exchange_name(account: APIKey) -> str:
    return {"bitget": "Bitget", "binance": "Binance"}.get((account.exchange or "").lower(), account.exchange or "your exchange")


async def _email(user: User, p: Purchase, account: APIKey, balance: float, needed: float) -> bool:
    from api.utils.links import build_app_link
    html, text = render(
        "low_balance",
        name=(user.email or "").split("@")[0],
        coin=p.crypto_symbol, amount_usdt=f"{float(p.amount_usd):.2f}", needed_usdt=f"{needed:.2f}",
        balance_usdt=f"{balance:.2f}", shortfall_usdt=f"{max(needed - balance, 0.0):.2f}",
        run_at=_fmt_utc(p.next_execution), frequency=p.frequency.replace("_", " "),
        exchange=_exchange_name(account),
        purchases_url=build_app_link("/purchases"),
        unsubscribe_url=build_app_link("/settings/notifications"),
    )
    return await email_service.queue(user.email, SUBJECT, html, text, template="low_balance",
                                     category="confirmation", user_id=user.id)


async def check_low_balances(db, now: Optional[datetime] = None, balance_reader: Optional[BalanceReader] = None) -> dict:
    """One pass over the active purchases whose next run is in a check window.
    Reads balances (one read per exchange account per pass), warns, clears.
    Commits the bookkeeping columns once at the end. Never raises for a
    single purchase: a failure is counted and reported, the pass continues."""
    now = now or utcnow()
    reader = balance_reader or read_usdt_balance
    out = Outcome()
    rows = (await db.execute(
        select(Purchase)
        .options(selectinload(Purchase.user), selectinload(Purchase.exchange_account))
        .where(Purchase.status == "active", Purchase.next_execution.is_not(None), Purchase.next_execution > now)
        .order_by(Purchase.next_execution.asc())
    )).scalars().all()
    balances: dict = {}          # account id -> float | Exception
    for p in rows:
        if in_window(p.next_execution, now) is None:
            continue
        user, account = p.user, p.exchange_account
        if user is None or user.suspended or user.kyc_status != "verified":
            continue   # the executor would skip it: a balance warning would only confuse
        if account is None or not account.verified:
            continue   # not a balance problem; the purchase already fails with a key message
        out.checked += 1
        if account.id not in balances:
            try:
                balances[account.id] = float(await asyncio.to_thread(reader, account))
            except Exception as exc:   # noqa: BLE001  (any client error: report, never raise)
                balances[account.id] = exc
        balance = balances[account.id]
        if isinstance(balance, Exception):
            out.unreadable += 1
            logger.warning("low-balance check: balance unreadable for purchase %s: %s", p.id, type(balance).__name__)
            await telegram.send(
                f"⚠️ Low-balance check could not read a balance\n{p.crypto_symbol} · {float(p.amount_usd):.2f} USDT · run {_fmt_utc(p.next_execution)}\n"
                f"{telegram.mask_email(user.email)} · {_exchange_name(account)}: {telegram.scrub(str(balance))}\nNo customer email sent.",
                f"low_balance_unreadable:{account.id}")
            continue
        needed = needed_usdt(p)
        if balance >= needed:
            out.sufficient += 1
            if p.low_balance_alerted_for is not None:
                p.low_balance_alerted_for = None   # topped up: the banner clears
                p.low_balance_alerted_at = None
                out.cleared += 1
            continue
        if low_balance_outstanding(p):
            out.already_alerted += 1   # one warning per run
            continue
        p.low_balance_alerted_for = p.next_execution
        p.low_balance_alerted_at = now
        out.alerted += 1
        emailed = False
        if Preferences.for_user(user).wants_email("email_low_balance"):
            try:
                emailed = await _email(user, p, account, balance, needed)
            except Exception as exc:   # noqa: BLE001
                logger.error("low-balance email not queued for purchase %s: %s", p.id, exc)
        await telegram.send(
            f"⚠️ Low balance before a buy\n{p.crypto_symbol} · needs ~{needed:.2f} USDT, has {balance:.2f} USDT (short {needed - balance:.2f})\n"
            f"{telegram.mask_email(user.email)} · run {_fmt_utc(p.next_execution)} · {'email queued' if emailed else 'no email (preference off or queue failed)'}",
            f"low_balance:{p.id}:{p.next_execution.isoformat()}")
    await db.commit()
    logger.info("low-balance check: %s", out.as_dict())
    return out.as_dict()
