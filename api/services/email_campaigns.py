"""Lifecycle email campaigns on top of EmailService + the template engine.

- welcome: sent right after registration (background task on the route)
- first_purchase / milestone: evaluated after each completed purchase
- purchase_summary: weekly cron (Mondays 09:00 UTC) via the scheduler
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from api.database import AsyncSessionLocal
from api.models import Transaction, User
from api.config import settings
from api.services.email_service import email_service
from api.services.email_templates import render

logger = logging.getLogger("autobuy.campaigns")

MILESTONES_USDT = (1_000, 10_000, 100_000, 1_000_000)


def _name(user: User) -> str:
    return user.email.split("@", 1)[0]


async def send_welcome(user_email: str) -> None:
    html, text = render("welcome", name=user_email.split("@", 1)[0])
    await email_service._send(user_email, f"Welcome to {settings.app_name}", html, text,
                              template="welcome")


async def after_purchase_completed(db, user: User, tx: Transaction) -> None:
    """First-purchase congratulations and invested-amount milestones."""
    completed_count = (await db.execute(
        select(func.count(Transaction.id)).where(
            Transaction.user_id == user.id,
            Transaction.status == "completed"))).scalar_one()
    if completed_count == 1:
        html, text = render(
            "first_purchase", name=_name(user), crypto=tx.crypto,
            amount_crypto=f"{tx.amount_crypto:.8f}",
            cost_usdt=f"{tx.cost_usdt:.2f}",
            exchange_rate=f"{tx.exchange_rate:.2f}" if tx.exchange_rate else "market",
            order_id=tx.binance_order_id or "-")
        await email_service._send(user.email, "🎉 Your first automated purchase",
                                  html, text, template="first_purchase")
        return

    total = float((await db.execute(
        select(func.coalesce(func.sum(Transaction.cost_usdt), 0.0)).where(
            Transaction.user_id == user.id,
            Transaction.status == "completed"))).scalar_one())
    crossed = [m for m in MILESTONES_USDT
               if total >= m and total - tx.cost_usdt < m]
    if crossed:
        milestone = f"{max(crossed):,}"
        html, text = render("milestone", name=_name(user), milestone=milestone)
        await email_service._send(user.email, f"🏆 {milestone} USDT invested",
                                  html, text, template="milestone")


async def on_buy_executed(db, user: User, tx, purchase) -> None:
    """First-purchase congratulations and invested-amount milestones, computed
    from purchase_transactions (the Auto-Buy history)."""
    from api.services import history
    count, total = await history.completed_count_and_total(db, user.id)
    cost = float(tx.cost_usd if tx.cost_usd is not None else purchase.amount_usd)
    if count == 1:
        html, text = render(
            "first_purchase", name=_name(user), crypto=purchase.crypto_symbol,
            amount_crypto=f"{(tx.amount_executed or 0):.8f}",
            cost_usdt=f"{cost:.2f}",
            exchange_rate=f"{tx.fill_price:.2f}" if tx.fill_price else "market",
            order_id=tx.order_id or "-")
        await email_service._send(user.email, "🎉 Your first automated purchase",
                                  html, text, template="first_purchase")
        return
    crossed = [m for m in MILESTONES_USDT if total >= m and total - cost < m]
    if crossed:
        milestone = f"{max(crossed):,}"
        html, text = render("milestone", name=_name(user), milestone=milestone)
        await email_service._send(user.email, f"🏆 {milestone} USDT invested",
                                  html, text, template="milestone")


async def send_weekly_summaries() -> int:
    """Monday-morning digest for every user with Auto-Buy activity in the last
    7 days. Returns how many digests were sent."""
    from api.services import history
    since = datetime.now(timezone.utc) - timedelta(days=7)
    sent = 0
    async with AsyncSessionLocal() as db:
        user_ids = await history.users_with_activity_since(db, since)
        for user_id in user_ids:
            user = (await db.execute(
                select(User).where(User.id == user_id))).scalar_one_or_none()
            if user is None:
                continue
            completed = await history.completed_rows(db, user_id, since=since)
            failed = await history.count_failed_since(db, since, user_id=user_id)
            if not completed and not failed:
                continue
            grouped = history.group_by_crypto(completed)
            rows = [{"crypto": c, "amount": f"{a:.8f}", "cost": f"{cost:.2f}"} for c, a, cost in grouped]
            total = sum(cost for _, _, cost in grouped)
            html, text = render(
                "purchase_summary", name=_name(user), rows=rows,
                total_usdt=f"{total:.2f}", count=len(rows), failed=failed)
            if await email_service._send(user.email, f"Your week at {settings.app_name}",
                                         html, text, template="purchase_summary"):
                sent += 1
    logger.info("weekly summaries sent to %d user(s)", sent)
    return sent
