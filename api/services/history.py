"""Purchase history: one read model over ``purchase_transactions``.

Every attempt the Auto-Buy executor records is a :class:`HistoryRow`, joined
with its purchase (coin, amount, wallet, network, type) and exchange account
(venue). Everything that shows history — the Transactions API, dashboard
holdings/averages/timeline, analytics, tax and performance reports, the admin
audit, the weekly digest and milestone emails — reads through this module,
so ``purchase_transactions`` is the single source of truth.

Status vocabulary: the table stores ``pending`` | ``success`` | ``failed``;
the API keeps the historical ``completed`` for ``success`` so clients and
filters keep working. Use :func:`to_api_status` / :func:`from_api_status`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import APIKey, Purchase, PurchaseTransaction

DB_SUCCESS = "success"
API_COMPLETED = "completed"
API_SIMULATED = "simulated"   # a dry-run withdrawal: recorded, nothing was sent
API_STATUSES = ("pending", "completed", "failed", "simulated")


def to_api_status(db_status: str) -> str:
    return API_COMPLETED if db_status == DB_SUCCESS else db_status


def from_api_status(api_status: str) -> str:
    return DB_SUCCESS if api_status in (API_COMPLETED, API_SIMULATED) else api_status


def is_simulated(tx: PurchaseTransaction) -> bool:
    return tx.tx_type == "withdrawal" and tx.status == DB_SUCCESS and (tx.raw_status or "") == "simulated"


def presented_status(tx: PurchaseTransaction) -> str:
    """The status shown everywhere (API, UI, emails, CSV): a dry-run
    withdrawal is "simulated", never "completed"."""
    return API_SIMULATED if is_simulated(tx) else to_api_status(tx.status)


def fmt_amount(value, digits: int = 8) -> str:
    """Crypto amounts for humans: at most `digits` decimals, trailing zeros trimmed
    ("0.00076179", "0.5", "0"). Storage keeps full precision."""
    if value is None:
        return ""
    text = f"{float(value):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass
class HistoryRow:
    id: UUID
    purchase_id: UUID
    user_id: UUID
    crypto: str
    purchase_type: str            # autobuy | dca
    tx_type: str                  # withdrawal | accumulation
    status: str                   # API vocabulary: pending | completed | failed | simulated (dry-run withdrawal)
    amount_crypto: float          # coins bought (0 when the buy failed)
    cost_usd: float               # quote spent (the purchase amount when the fill did not report it)
    fill_price: Optional[float]
    fee: Optional[float]
    fee_currency: Optional[str]
    exchange: Optional[str]
    order_id: Optional[str]
    withdrawal_id: Optional[str]
    wallet_address: Optional[str]
    blockchain_network: Optional[str]
    withdrawal_fee: Optional[float]
    withdrawal_fee_currency: Optional[str]
    tx_hash: Optional[str]
    error_message: Optional[str]
    executed_at: Optional[datetime]
    created_at: datetime

    @property
    def happened_at(self) -> datetime:
        """When the attempt took effect: executed_at, else created_at."""
        return self.executed_at or self.created_at

    @property
    def exchange_rate(self) -> Optional[float]:
        if self.fill_price is not None:
            return self.fill_price
        if self.amount_crypto:
            return self.cost_usd / self.amount_crypto
        return None


BUY_TYPES = ("buy", "accumulation")   # rows that are purchases (lots); "withdrawal" rows move coins


def _row(tx: PurchaseTransaction, p: Purchase, exchange: Optional[str]) -> HistoryRow:
    amount = float(tx.amount_executed or 0.0)
    if tx.cost_usd is not None:
        cost = tx.cost_usd
    else:
        cost = float(p.amount_usd) if tx.tx_type in BUY_TYPES else 0.0
    return HistoryRow(
        id=tx.id, purchase_id=p.id, user_id=p.user_id, crypto=p.crypto_symbol,
        purchase_type=p.purchase_type, tx_type=tx.tx_type, status=presented_status(tx),
        amount_crypto=amount, cost_usd=float(cost), fill_price=tx.fill_price,
        fee=tx.fee, fee_currency=tx.fee_currency, exchange=exchange,
        order_id=tx.order_id or (tx.exchange_tx_id if tx.tx_type == "accumulation" else None),
        withdrawal_id=tx.withdrawal_id or (tx.exchange_tx_id if tx.tx_type == "withdrawal" else None),
        wallet_address=p.wallet_address if tx.tx_type == "withdrawal" else None,
        blockchain_network=(tx.withdrawal_chain or p.blockchain_network) if tx.tx_type == "withdrawal" else None,
        withdrawal_fee=tx.withdrawal_fee, withdrawal_fee_currency=tx.withdrawal_fee_currency,
        tx_hash=tx.tx_hash, error_message=tx.error_message,
        executed_at=tx.executed_at, created_at=tx.created_at,
    )


def _base():
    return (select(PurchaseTransaction, Purchase, APIKey.exchange)
            .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
            .outerjoin(APIKey, Purchase.exchange_account_id == APIKey.id))


def _filters(user_id=None, status: Optional[str] = None, crypto: Optional[str] = None,
             since: Optional[datetime] = None, until: Optional[datetime] = None) -> list:
    where = []
    if user_id is not None:
        where.append(Purchase.user_id == user_id)
    if status is not None:
        where.append(PurchaseTransaction.status == from_api_status(status))
        simulated = (PurchaseTransaction.tx_type == "withdrawal") & (PurchaseTransaction.raw_status == "simulated")
        if status == API_SIMULATED:
            where.append(simulated)
        elif status == API_COMPLETED:
            where.append(~simulated)
    if crypto is not None:
        where.append(Purchase.crypto_symbol == crypto.strip().upper())
    if since is not None:
        where.append(PurchaseTransaction.created_at >= since)
    if until is not None:
        where.append(PurchaseTransaction.created_at < until)
    return where


async def _rows(db: AsyncSession, stmt) -> List[HistoryRow]:
    return [_row(tx, p, ex) for tx, p, ex in (await db.execute(stmt)).all()]


# ------------------------------------------------------------------ listing

async def list_rows(db: AsyncSession, user_id=None, *, status: Optional[str] = None,
                    crypto: Optional[str] = None, since: Optional[datetime] = None,
                    limit: int = 50, offset: int = 0) -> Tuple[List[HistoryRow], int]:
    """Newest first. ``user_id=None`` lists every user (admin audit)."""
    where = _filters(user_id, status, crypto, since)
    total = (await db.execute(
        select(func.count(PurchaseTransaction.id))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id).where(*where))).scalar_one()
    rows = await _rows(db, _base().where(*where)
                       .order_by(PurchaseTransaction.created_at.desc()).limit(limit).offset(offset))
    return rows, total


async def get_row(db: AsyncSession, user_id, tx_id: UUID) -> Optional[HistoryRow]:
    rows = await _rows(db, _base().where(Purchase.user_id == user_id, PurchaseTransaction.id == tx_id))
    return rows[0] if rows else None


async def completed_rows(db: AsyncSession, user_id, *, since: Optional[datetime] = None,
                         until: Optional[datetime] = None) -> List[HistoryRow]:
    """Successful buys, oldest first — the input for holdings, lots and ROI.
    Withdrawal rows are not purchases and are excluded."""
    where = _filters(user_id, API_COMPLETED, None, since, until) + [PurchaseTransaction.tx_type.in_(BUY_TYPES)]
    return await _rows(db, _base().where(*where).order_by(PurchaseTransaction.created_at))


# ------------------------------------------------------------------ aggregates

async def stats(db: AsyncSession, user_id) -> dict:
    ok = (PurchaseTransaction.status == DB_SUCCESS) & PurchaseTransaction.tx_type.in_(BUY_TYPES)
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    row = (await db.execute(
        select(func.count(PurchaseTransaction.id),
               func.coalesce(func.sum(cost).filter(ok), 0.0),
               func.coalesce(func.avg(cost).filter(ok), 0.0),
               func.count(PurchaseTransaction.id).filter(ok),
               func.count(PurchaseTransaction.id).filter(PurchaseTransaction.status == "failed"),
               func.count(PurchaseTransaction.id).filter(PurchaseTransaction.status == "pending"))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id))).one()
    return {"total_transactions": row[0], "total_invested_usdt": round(float(row[1]), 8),
            "average_purchase_usdt": round(float(row[2]), 8), "completed_count": row[3],
            "failed_count": row[4], "pending_count": row[5]}


async def holdings(db: AsyncSession, user_id) -> List[Tuple[str, float, float]]:
    """[(crypto, total_amount, total_cost_usd)] over successful buys, biggest cost first."""
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    rows = (await db.execute(
        select(Purchase.crypto_symbol,
               func.coalesce(func.sum(PurchaseTransaction.amount_executed), 0.0),
               func.coalesce(func.sum(cost), 0.0))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == DB_SUCCESS,
               PurchaseTransaction.tx_type.in_(BUY_TYPES))
        .group_by(Purchase.crypto_symbol)
        .order_by(func.sum(cost).desc()))).all()
    return [(r[0], float(r[1]), float(r[2])) for r in rows if float(r[1]) > 0]


async def monthly_average(db: AsyncSession, user_id) -> float:
    """Average spent per calendar month, over months with a successful buy."""
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    month = func.date_trunc("month", PurchaseTransaction.created_at)
    rows = (await db.execute(
        select(month, func.sum(cost))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == DB_SUCCESS,
               PurchaseTransaction.tx_type.in_(BUY_TYPES))
        .group_by(month))).all()
    if not rows:
        return 0.0
    return round(sum(float(r[1]) for r in rows) / len(rows), 8)


async def timeline(db: AsyncSession, user_id, days: int, now: Optional[datetime] = None) -> List[dict]:
    """Daily count + spend of successful buys for the last ``days`` days, zero-filled."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    day = func.date_trunc("day", PurchaseTransaction.created_at)
    rows = (await db.execute(
        select(day, func.count(PurchaseTransaction.id), func.sum(cost))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == DB_SUCCESS,
               PurchaseTransaction.tx_type.in_(BUY_TYPES), PurchaseTransaction.created_at >= start)
        .group_by(day))).all()
    by_day = {r[0].date(): (r[1], float(r[2])) for r in rows}
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        count, total = by_day.get(d, (0, 0.0))
        out.append({"date": d, "transaction_count": count, "total_usdt": round(total, 8)})
    return out


async def count_failed_since(db: AsyncSession, since: datetime, user_id=None) -> int:
    where = [PurchaseTransaction.status == "failed", PurchaseTransaction.created_at >= since]
    stmt = select(func.count(PurchaseTransaction.id))
    if user_id is not None:
        stmt = stmt.join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        where.append(Purchase.user_id == user_id)
    return (await db.execute(stmt.where(*where))).scalar_one()


async def completed_count_and_total(db: AsyncSession, user_id) -> Tuple[int, float]:
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    row = (await db.execute(
        select(func.count(PurchaseTransaction.id), func.coalesce(func.sum(cost), 0.0))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == DB_SUCCESS,
               PurchaseTransaction.tx_type.in_(BUY_TYPES)))).one()
    return int(row[0]), float(row[1])


async def users_with_activity_since(db: AsyncSession, since: datetime) -> Sequence[UUID]:
    return (await db.execute(
        select(Purchase.user_id)
        .join(PurchaseTransaction, PurchaseTransaction.purchase_id == Purchase.id)
        .where(PurchaseTransaction.created_at >= since)
        .group_by(Purchase.user_id))).scalars().all()


def group_by_crypto(rows: Iterable[HistoryRow]) -> List[Tuple[str, float, float]]:
    """[(crypto, amount, cost)] from already-loaded successful rows."""
    acc: dict = {}
    for r in rows:
        a, c = acc.get(r.crypto, (0.0, 0.0))
        acc[r.crypto] = (a + r.amount_crypto, c + r.cost_usd)
    return [(k, v[0], v[1]) for k, v in sorted(acc.items())]


# ------------------------------------------------------------------ platform-wide (admin)

async def platform_totals(db: AsyncSession, day_ago: datetime, week_ago: datetime) -> dict:
    ok = (PurchaseTransaction.status == DB_SUCCESS) & PurchaseTransaction.tx_type.in_(BUY_TYPES)
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    row = (await db.execute(
        select(func.coalesce(func.sum(cost).filter(ok), 0.0),
               func.count(PurchaseTransaction.id),
               func.count(PurchaseTransaction.id).filter(PurchaseTransaction.status == "failed",
                                                         PurchaseTransaction.created_at >= day_ago),
               func.coalesce(func.sum(cost).filter(ok, PurchaseTransaction.created_at >= week_ago), 0.0))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id))).one()
    return {"total_invested_usdt": round(float(row[0]), 2), "total_transactions": int(row[1]),
            "failed_transactions_24h": int(row[2]), "volume_7d_usdt": round(float(row[3]), 2)}


async def invested_per_user(db: AsyncSession) -> dict:
    """{user_id: total spent on successful buys}."""
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    rows = (await db.execute(
        select(Purchase.user_id, func.coalesce(func.sum(cost), 0.0))
        .join(PurchaseTransaction, PurchaseTransaction.purchase_id == Purchase.id)
        .where(PurchaseTransaction.status == DB_SUCCESS, PurchaseTransaction.tx_type.in_(BUY_TYPES))
        .group_by(Purchase.user_id))).all()
    return {r[0]: round(float(r[1]), 2) for r in rows}


async def withdrawals_by_parent(db: AsyncSession, user_id, parent_ids) -> dict:
    """{buy row id: withdrawal HistoryRow} for the buys that have a withdrawal row (any status)."""
    ids = [i for i in parent_ids]
    if not ids:
        return {}
    rows = await _rows(db, _base().where(Purchase.user_id == user_id, PurchaseTransaction.tx_type == "withdrawal",
                                          PurchaseTransaction.parent_transaction_id.in_(ids))
                       .order_by(PurchaseTransaction.created_at))
    out = {}
    parents = {tx.id: tx.parent_transaction_id for tx, _, _ in (await db.execute(
        select(PurchaseTransaction, Purchase, APIKey.exchange)
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .outerjoin(APIKey, Purchase.exchange_account_id == APIKey.id)
        .where(PurchaseTransaction.parent_transaction_id.in_(ids)))).all()}
    for r in rows:
        out[parents.get(r.id)] = r   # the latest withdrawal attempt wins
    return out
