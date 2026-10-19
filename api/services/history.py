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

from sqlalchemy import case, func, select
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


def _trim(text: str, keep: int = 0) -> str:
    """Trim trailing zeros past `keep` decimals ("1.50000000" -> "1.5"; keep=2: "1.500" -> "1.50")."""
    if "." not in text:
        return text
    whole, frac = text.split(".")
    frac = frac.rstrip("0")
    if len(frac) < keep:
        frac = frac + "0" * (keep - len(frac))
    return f"{whole}.{frac}" if frac else whole


def fmt_amount_plain(value, digits: int = 8) -> str:
    """Machine-readable coin amount (CSV): at most `digits` decimals, trailing zeros
    trimmed, no grouping, dot decimal point ("0.00076179", "2389380.228", "0")."""
    if value is None:
        return ""
    return _trim(f"{float(value):.{digits}f}")


def fmt_amount(value, digits: int = 8) -> str:
    """Coin amounts for humans (web, email, Telegram, PDF): thousands separators;
    at or above 1,000 coins at most 2 decimals ("2,389,380.23 PEPE"), below that
    at most `digits` decimals, trailing zeros trimmed ("0.00076179", "0.5", "0").
    Storage keeps full precision."""
    if value is None:
        return ""
    v = float(value)
    if abs(v) >= 1000:
        return _trim(f"{v:,.2f}")
    return _trim(f"{v:,.{digits}f}")


def _price_decimals(value: float, min_significant: int = 4) -> int:
    """Decimals that show at least `min_significant` significant digits, never fewer than 2, at most 10."""
    import math
    if value == 0:
        return 2
    return max(2, min(10, min_significant - 1 - math.floor(math.log10(abs(value)))))


def fmt_price_plain(value) -> str:
    """Machine-readable price (CSV): at least 4 significant digits, no grouping ("84487.36", "0.000004181")."""
    if value is None:
        return ""
    v = float(value)
    return _trim(f"{v:.{_price_decimals(v)}f}", keep=2)


def fmt_price(value) -> str:
    """Price for humans: at least 4 significant digits, never fewer than 2 decimals,
    thousands separators ("84,487.36", "0.09179", "0.000004181")."""
    if value is None:
        return ""
    v = float(value)
    return _trim(f"{v:,.{_price_decimals(v)}f}", keep=2)


@dataclass
class HistoryRow:
    id: UUID
    purchase_id: UUID
    user_id: UUID
    crypto: str
    purchase_type: str            # autobuy | dca
    tx_type: str                  # withdrawal | accumulation
    status: str                   # API vocabulary: pending | completed | failed | simulated (dry-run withdrawal)
    amount_crypto: float          # coins bought, GROSS fill as Bitget's order history shows it (0 when the buy failed)
    net_amount: float             # what the account actually received: gross minus the trading fee when it was charged in the coin
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


def net_of_fee(tx: PurchaseTransaction, coin: str) -> float:
    """Coins the account actually received from a buy: the gross fill minus the
    trading fee when Bitget charged it in the coin itself (proved against the
    live balance on 2026-09-21: 0.000197 BTC bought, fee 1.97e-07 BTC, balance
    0.000196803). Fees charged in USDT or BGB do not touch the quantity."""
    if tx.tx_type not in BUY_TYPES:
        return float(tx.amount_executed or 0.0)
    amount = float(tx.amount_executed or 0.0)
    if tx.fee and (tx.fee_currency or "").upper() == (coin or "").upper():
        amount = max(amount - float(tx.fee), 0.0)
    return amount


def withdrawal_fee_mode(tx: PurchaseTransaction) -> Optional[str]:
    """The fee mode a withdrawal row was SENT under, never today's configuration.
    Since migration 025 the executor stores it in withdrawal_fee_mode; rows
    older than that are derived from what they recorded at send time:
      exclusive: Bitget charged the fee on top -> expected_received == amount_executed
      inclusive: Bitget took the fee out of the size -> expected_received == amount_executed - fee
    None when nothing was recorded (pre-2026-09-14 rows)."""
    if tx.tx_type != "withdrawal":
        return None
    if tx.withdrawal_fee_mode in ("exclusive", "inclusive"):
        return tx.withdrawal_fee_mode
    if tx.expected_received is None or tx.amount_executed is None:
        return None
    if abs(float(tx.expected_received) - float(tx.amount_executed)) < 1e-12:
        return "exclusive"
    return "inclusive"


def withdrawal_movement(tx: PurchaseTransaction) -> Tuple[float, float, Optional[str]]:
    """(left_exchange, received_by_wallet, mode) for a successful, real withdrawal.
    exclusive: left = size + fee, received = size.  inclusive: left = size, received = size - fee.
    A row with no recorded mode uses the mode configured NOW and is reported as such
    (only pre-2026-09-14 simulated rows on the dev database; none in production)."""
    size = float(tx.amount_executed or 0.0)
    fee = float(tx.withdrawal_fee or 0.0)
    mode = withdrawal_fee_mode(tx)
    if mode is None:
        from api.services.bitget_market import fee_mode
        mode = fee_mode()
    if mode == "exclusive":
        return size + fee, size, mode
    return size, max(size - fee, 0.0), mode


def _row(tx: PurchaseTransaction, p: Purchase, exchange: Optional[str]) -> HistoryRow:
    amount = float(tx.amount_executed or 0.0)
    if tx.cost_usd is not None:
        cost = tx.cost_usd
    elif tx.tx_type in BUY_TYPES and tx.status == DB_SUCCESS:
        cost = float(p.amount_usd)   # older successful buys without a recorded quote
    else:
        cost = 0.0   # a failed or pending attempt spent nothing
    return HistoryRow(
        id=tx.id, purchase_id=p.id, user_id=p.user_id, crypto=p.crypto_symbol,
        purchase_type=p.purchase_type, tx_type=tx.tx_type, status=presented_status(tx),
        amount_crypto=amount, net_amount=net_of_fee(tx, p.crypto_symbol), cost_usd=float(cost), fill_price=tx.fill_price,
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
    """The rule: total_invested_usdt, average_purchase_usdt and completed_count
    are computed over the SAME set of rows — successful buys only. Failed and
    pending attempts and withdrawal rows never add cost and are never averaged;
    they are reported separately (failed_count, pending_count) and
    total_transactions counts every row of every kind for the history table."""
    ok = (PurchaseTransaction.status == DB_SUCCESS) & PurchaseTransaction.tx_type.in_(BUY_TYPES)
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    row = (await db.execute(
        select(func.count(PurchaseTransaction.id),
               func.coalesce(func.sum(cost).filter(ok), 0.0),
               func.coalesce(func.avg(cost).filter(ok), 0.0),
               func.count(PurchaseTransaction.id).filter(ok),
               func.count(PurchaseTransaction.id).filter(PurchaseTransaction.status == "failed",
                                                          PurchaseTransaction.tx_type.in_(BUY_TYPES)),
               func.count(PurchaseTransaction.id).filter(PurchaseTransaction.status == "pending"),
               func.count(PurchaseTransaction.id).filter(PurchaseTransaction.status == "needs_review"))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id))).one()
    return {"total_transactions": row[0], "total_invested_usdt": round(float(row[1]), 8),
            "average_purchase_usdt": round(float(row[2]), 8), "completed_count": row[3],
            "failed_count": row[4], "pending_count": row[5], "needs_review_count": row[6]}


async def count_network_failures_since(db: AsyncSession, since: datetime) -> int:
    """Failed buy attempts caused by the exchange being unreachable (DNS, timeouts,
    connection errors) rather than by the order itself — a run that silently
    could not happen."""
    from sqlalchemy import or_
    err = PurchaseTransaction.error_message
    q = (select(func.count(PurchaseTransaction.id))
         .where(PurchaseTransaction.status == "failed", PurchaseTransaction.tx_type.in_(BUY_TYPES),
                PurchaseTransaction.created_at >= since,
                or_(err.ilike("%NETWORK%"), err.ilike("%name resolution%"), err.ilike("%timed out%"),
                    err.ilike("%ConnectionError%"), err.ilike("%unreachable%"))))
    return int((await db.execute(q)).scalar_one())


@dataclass
class Position:
    """What a customer owns of one coin and where it is. All three quantities
    come from recorded rows only (see net_of_fee / withdrawal_movement)."""
    crypto: str
    acquired: float          # Σ net buys: gross fill − trading fee charged in the coin
    on_exchange: float       # acquired − Σ what each real withdrawal took off the Bitget balance
    sent_to_wallet: float    # Σ what each real withdrawal delivered to the destination
    cost_usd: float          # Σ quote spent on those buys
    withdrawals: int         # real (non-simulated) successful withdrawals counted
    fee_modes: List[str]     # distinct modes those withdrawals were sent under


async def positions(db: AsyncSession, user_id) -> List[Position]:
    """Per coin, biggest cost first. Buys are summed net of the coin-denominated
    trading fee; successful real withdrawals are subtracted per the mode each
    was sent under. Simulated (dry-run) withdrawals move nothing."""
    cost = func.coalesce(PurchaseTransaction.cost_usd, Purchase.amount_usd)
    fee_in_coin = case((func.upper(func.coalesce(PurchaseTransaction.fee_currency, "")) == func.upper(Purchase.crypto_symbol),
                        func.coalesce(PurchaseTransaction.fee, 0.0)), else_=0.0)
    net = func.coalesce(PurchaseTransaction.amount_executed, 0.0) - fee_in_coin
    rows = (await db.execute(
        select(Purchase.crypto_symbol, func.coalesce(func.sum(net), 0.0), func.coalesce(func.sum(cost), 0.0))
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == DB_SUCCESS,
               PurchaseTransaction.tx_type.in_(BUY_TYPES))
        .group_by(Purchase.crypto_symbol)
        .order_by(func.sum(cost).desc()))).all()
    out = {r[0]: Position(crypto=r[0], acquired=float(r[1]), on_exchange=float(r[1]), sent_to_wallet=0.0,
                          cost_usd=float(r[2]), withdrawals=0, fee_modes=[]) for r in rows if float(r[1]) > 0}
    wds = (await db.execute(
        select(PurchaseTransaction, Purchase.crypto_symbol)
        .join(Purchase, PurchaseTransaction.purchase_id == Purchase.id)
        .where(Purchase.user_id == user_id, PurchaseTransaction.status == DB_SUCCESS,
               PurchaseTransaction.tx_type == "withdrawal"))).all()
    for tx, coin in wds:
        if is_simulated(tx) or coin not in out:
            continue
        left, received, mode = withdrawal_movement(tx)
        pos = out[coin]
        pos.on_exchange = max(pos.on_exchange - left, 0.0)
        pos.sent_to_wallet += received
        pos.withdrawals += 1
        if mode not in pos.fee_modes:
            pos.fee_modes.append(mode)
    for pos in out.values():
        pos.acquired, pos.on_exchange, pos.sent_to_wallet = (round(pos.acquired, 10), round(pos.on_exchange, 10),
                                                             round(pos.sent_to_wallet, 10))
    return list(out.values())


async def holdings(db: AsyncSession, user_id) -> List[Tuple[str, float, float]]:
    """[(crypto, acquired, total_cost_usd)] over successful buys, NET of the
    coin-denominated trading fee, biggest cost first. Feeds P&L, average buy
    price and cost shares: invested cost bought the acquired quantity."""
    return [(p.crypto, p.acquired, p.cost_usd) for p in await positions(db, user_id)]


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
    """[(crypto, net amount, cost)] from already-loaded successful rows (net of the coin fee, like holdings)."""
    acc: dict = {}
    for r in rows:
        a, c = acc.get(r.crypto, (0.0, 0.0))
        acc[r.crypto] = (a + r.net_amount, c + r.cost_usd)
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
