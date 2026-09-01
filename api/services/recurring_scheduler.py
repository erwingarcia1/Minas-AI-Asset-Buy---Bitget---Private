"""Background scheduler that executes due recurring purchases.

One APScheduler interval job ("tick") polls for enabled purchases whose
next_purchase_date has arrived and runs each through PurchaseService. A
Postgres advisory lock makes ticks single-instance safe across replicas.
Live trading requires AUTOBUY_DRY_RUN=0; the default simulates orders.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text

from api.database import AsyncSessionLocal, engine
from api.models import RecurringPurchase
from api.services.purchase_service import PurchaseService

logger = logging.getLogger("autobuy.scheduler")

ADVISORY_LOCK_KEY = 0x41427579  # "ABuy"
FAILURE_RETRY = timedelta(hours=1)


def add_months(dt: datetime, months: int = 1) -> datetime:
    """Calendar-aware month addition; clamps to the last day of short months
    (Jan 31 -> Feb 28, Aug 31 -> Sep 30)."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    # last day of the target month
    next_first = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_first - timedelta(days=1)).day
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def next_run_after(frequency: str, from_dt: datetime) -> datetime:
    """The next due date one interval after `from_dt`."""
    if frequency == "daily":
        return from_dt + timedelta(days=1)
    if frequency == "weekly":
        return from_dt + timedelta(days=7)
    if frequency == "biweekly":
        return from_dt + timedelta(days=14)
    if frequency == "monthly":
        return add_months(from_dt, 1)
    raise ValueError(f"unknown frequency {frequency!r}")


class RecurringScheduler:
    def __init__(self, poll_seconds: int = 60, dry_run: bool = True):
        self.poll_seconds = poll_seconds
        self.dry_run = dry_run
        self.last_tick_at: Optional[datetime] = None
        self._scheduler: Optional[AsyncIOScheduler] = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(self.tick, "interval", seconds=self.poll_seconds,
                                max_instances=1, coalesce=True)
        self._scheduler.start()
        logger.info("scheduler started (poll=%ss, dry_run=%s)",
                    self.poll_seconds, self.dry_run)

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("scheduler stopped")

    async def tick(self) -> int:
        """Execute every due purchase once. Returns how many were processed."""
        self.last_tick_at = datetime.now(timezone.utc)
        # Hold the advisory lock on a dedicated connection for the whole tick so
        # two replicas can never process the same due purchases.
        async with engine.connect() as lock_conn:
            got = (await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            )).scalar()
            if not got:
                logger.info("tick skipped: another instance holds the lock")
                return 0
            try:
                return await self._process_due()
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
                await lock_conn.commit()

    async def _process_due(self) -> int:
        now = datetime.now(timezone.utc)
        processed = 0
        async with AsyncSessionLocal() as db:
            due = (await db.execute(
                select(RecurringPurchase).where(
                    RecurringPurchase.enabled.is_(True),
                    RecurringPurchase.next_purchase_date <= now,
                ).order_by(RecurringPurchase.next_purchase_date))).scalars().all()

            service = PurchaseService(db)
            for purchase in due:
                try:
                    tx = await service.execute_purchase(
                        purchase.user_id, purchase.id,
                        dry_run=self.dry_run, due_date=purchase.next_purchase_date)
                except Exception:
                    logger.exception("purchase %s: unexpected executor error", purchase.id)
                    continue

                if tx.status == "completed":
                    # advance from the due date; skip any missed intervals so a
                    # long outage cannot trigger a catch-up buying spree
                    nxt = next_run_after(purchase.frequency, purchase.next_purchase_date)
                    while nxt <= now:
                        nxt = next_run_after(purchase.frequency, nxt)
                    purchase.next_purchase_date = nxt
                else:
                    # failed: retry in an hour, not on the next 60s tick
                    purchase.next_purchase_date = now + FAILURE_RETRY
                await db.commit()
                processed += 1
        if processed:
            logger.info("tick processed %d due purchase(s)", processed)
        return processed


# Singleton wired into the app lifespan; /health reports its state.
scheduler = RecurringScheduler()
