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
from api.services.email_service import email_service
from api.services.email_templates import render

logger = logging.getLogger("autobuy.campaigns")

MILESTONES_USDT = (1_000, 10_000, 100_000, 1_000_000)


def _name(user: User) -> str:
    return user.email.split("@", 1)[0]


async def send_welcome(user_email: str) -> None:
    html, text = render("welcome", name=user_email.split("@", 1)[0])
    await email_service._send(user_email, "Welcome to AutoBuy", html, text,
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


async def send_weekly_summaries() -> None:
    """Monday-morning digest for every user with activity in the last 7 days."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with AsyncSessionLocal() as db:
        user_ids = (await db.execute(
            select(Transaction.user_id).where(Transaction.created_at >= since)
            .group_by(Transaction.user_id))).scalars().all()
        for user_id in user_ids:
            user = (await db.execute(
                select(User).where(User.id == user_id))).scalar_one_or_none()
            if user is None:
                continue
            rows_q = (await db.execute(
                select(Transaction.crypto,
                       func.sum(Transaction.amount_crypto),
                       func.sum(Transaction.cost_usdt))
                .where(Transaction.user_id == user_id,
                       Transaction.created_at >= since,
                       Transaction.status == "completed")
                .group_by(Transaction.crypto))).all()
            failed = (await db.execute(
                select(func.count(Transaction.id)).where(
                    Transaction.user_id == user_id,
                    Transaction.created_at >= since,
                    Transaction.status == "failed"))).scalar_one()
            if not rows_q and not failed:
                continue
            rows = [{"crypto": r[0], "amount": f"{float(r[1]):.8f}",
                     "cost": f"{float(r[2]):.2f}"} for r in rows_q]
            total = sum(float(r[2]) for r in rows_q)
            html, text = render(
                "purchase_summary", name=_name(user), rows=rows,
                total_usdt=f"{total:.2f}", count=len(rows), failed=failed,
                show_unsubscribe=True)
            await email_service._send(user.email, "Your week at AutoBuy",
                                      html, text, template="purchase_summary")
    logger.info("weekly summaries sent to %d user(s)", len(user_ids))
