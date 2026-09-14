"""Admin alert evaluation (docs/ALERTING.md conditions the app can see) and the
critical-alert email digest: with or without Slack, a critical condition
reaches the support inbox and every admin by email, at most once per
condition every ALERT_EMAIL_REPEAT_HOURS."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models import Purchase, User
from api.services import history

logger = logging.getLogger("autobuy.alerts")

ALERT_EMAIL_REPEAT_HOURS = 6
NOTIFIED_KEY = "autobuy:alerts:emailed:{name}"


class AlertItem(BaseModel):
    name: str
    severity: str  # ok | warning | critical
    value: str
    detail: str


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _severity_for_failures(fails: Optional[int]) -> str:
    if fails is None:
        return "warning"
    return "critical" if fails >= 5 else "warning" if fails >= 2 else "ok"


async def compute_alerts(db: AsyncSession) -> List[AlertItem]:
    """Live evaluation of the docs/ALERTING.md conditions the app can see for
    the Auto-Buy execute_purchases job. Heartbeats come from Redis, so they
    describe whichever worker actually ran, not the one answering."""
    from api.services import scheduler_state
    from api.tasks.purchase_scheduler import INTERVAL_MINUTES, MISFIRE_GRACE_SECONDS

    now = datetime.now(timezone.utc)
    items = []

    # -- pause switch (both schedulers) --
    try:
        pause = await scheduler_state.pause_info()
        items.append(AlertItem(
            name="schedulers_paused",
            severity="warning" if pause["paused"] else "ok",
            value="paused" if pause["paused"] else "running",
            detail=("Admin pause switch for the Auto-Buy job"
                    + (f"; paused by {pause['by']} since {pause['since']}" if pause["paused"] else ""))))
    except scheduler_state.PauseStateUnavailable:
        pause = {"paused": None}
        items.append(AlertItem(
            name="schedulers_paused", severity="critical", value="unknown",
            detail="Redis unreadable: the Auto-Buy job fails closed and skips every run until it is back"))

    # -- email delivery (outbox give-ups this hour) --
    from api.services import email_outbox
    failed_emails = await email_outbox.failures_this_hour()
    threshold = email_outbox.failure_alert_threshold()
    items.append(AlertItem(
        name="email_delivery_failures",
        severity="warning" if failed_emails is None else "critical" if failed_emails > threshold else "ok",
        value="unknown" if failed_emails is None else str(failed_emails),
        detail=(f"Emails given up on after all retries this hour (critical above {threshold}); "
                "see GET /api/admin/emails/undelivered")))

    # -- Auto-Buy execute_purchases job --
    ab = await scheduler_state.status("autobuy")
    ab_fails = ab["consecutive_failures"]
    items.append(AlertItem(
        name="autobuy_consecutive_failures",
        severity=_severity_for_failures(ab_fails),
        value="unknown" if ab_fails is None else str(ab_fails),
        detail="Auto-Buy job: consecutive runs that raised or failed every due purchase (critical at 5)"))

    overdue_after = timedelta(minutes=INTERVAL_MINUTES * 2, seconds=MISFIRE_GRACE_SECONDS)
    ab_last = _parse_iso(ab["last_run_at"])
    if ab_last is None:
        ab_sev = "warning"
    elif now - ab_last > overdue_after:
        ab_sev = "critical"
    elif ab["last_result"] in ("error", "failed"):
        ab_sev = "warning"
    else:
        ab_sev = "ok"
    items.append(AlertItem(
        name="autobuy_run",
        severity=ab_sev,
        value=ab["last_run_at"] or "never",
        detail=(f"Auto-Buy job: last execute_purchases run (every {INTERVAL_MINUTES} min; "
                f"critical if silent >{int(overdue_after.total_seconds() // 60)} min)"
                + (f"; last result {ab['last_result']}" if ab["last_result"] else "")
                + (f" {ab['last_counts']}" if ab["last_counts"] else ""))))

    # -- failed Auto-Buy execution attempts in the last 24h (purchase_transactions) --
    from api.services import history
    # -- fill deviations (warning band) --
    from api.services import fill_watch
    deviations = await fill_watch.count_24h()
    items.append(AlertItem(
        name="fill_deviations",
        severity=("warning" if deviations is None else "critical" if deviations >= fill_watch.CRITICAL_ALERT_THRESHOLD
                  else "warning" if deviations >= fill_watch.WARN_ALERT_THRESHOLD else "ok"),
        value="unknown" if deviations is None else str(deviations),
        detail=(f"Successful buys in the last day whose fill deviated more than FILL_WARN_PCT ({settings.fill_warn_pct:g}%) "
                f"from the expected amount (warning at {fill_watch.WARN_ALERT_THRESHOLD}, critical at "
                f"{fill_watch.CRITICAL_ALERT_THRESHOLD}); details at GET /api/admin/fills/deviations")))

    # -- stalled / missed recurring purchases --
    from api.tasks.purchase_scheduler import MAX_ATTEMPTS
    stopped = (await db.execute(select(func.count(Purchase.id)).where(
        Purchase.status == "failed", Purchase.failure_count >= MAX_ATTEMPTS,
        Purchase.updated_at >= now - timedelta(days=7)))).scalar_one()
    items.append(AlertItem(
        name="stopped_purchases",
        severity="critical" if stopped else "ok", value=str(stopped),
        detail="Purchases stopped in the last 7 days after repeated failed attempts (one-time after 3 attempts, "
               "recurring after 3 missed cycles): the customer has stopped buying and must recreate or be re-armed"))
    missed = (await db.execute(select(func.count(Purchase.id)).where(
        Purchase.status == "active", Purchase.failure_count >= MAX_ATTEMPTS,
        Purchase.updated_at >= now - timedelta(days=1)))).scalar_one()
    items.append(AlertItem(
        name="missed_cycles",
        severity="warning" if missed else "ok", value=str(missed),
        detail="Recurring purchases that missed a whole cycle in the last day (3 failed attempts) and moved on to their "
               "next interval; they stop after 3 missed cycles in a row"))
    parked = (await db.execute(select(func.count(Purchase.id)).where(Purchase.status == "needs_review"))).scalar_one()
    items.append(AlertItem(
        name="purchases_needing_review",
        severity="critical" if parked else "ok", value=str(parked),
        detail="Purchases parked as needs_review (buy outcome unknown or fill implausible): resolve them at "
               "GET /api/admin/purchases?status=needs_review -> /review -> /resolve; they never retry on their own"))

    network_1h = await history.count_network_failures_since(db, now - timedelta(hours=1))
    items.append(AlertItem(
        name="exchange_unreachable",
        severity="critical" if network_1h >= 5 else "warning" if network_1h >= 2 else "ok",
        value=str(network_1h),
        detail=("Buy attempts in the last hour that failed because the exchange could not be reached "
                "(DNS, timeout, connection): missed buys the customer is emailed about; warning at 2, critical at 5")))
    failed_24h = await history.count_failed_since(db, now - timedelta(days=1))
    items.append(AlertItem(
        name="failed_transactions_24h",
        severity="warning" if failed_24h > 10 else "ok",
        value=str(failed_24h), detail="Auto-Buy job: failed execution attempts in the last 24h"))

    return items


async def admin_recipients(db: AsyncSession) -> List[str]:
    """Support inbox plus every admin account's email, de-duplicated."""
    out: List[str] = []
    if settings.support_email:
        out.append(settings.support_email)
    admins = (await db.execute(select(User.email).where(User.role == "admin", User.suspended == False))).scalars().all()  # noqa: E712
    for e in admins:
        if e and e not in out:
            out.append(e)
    return out


async def notify_critical_alerts(db: AsyncSession, now: Optional[datetime] = None) -> dict:
    """Email the admins about critical alerts that were not emailed in the last
    ALERT_EMAIL_REPEAT_HOURS. Returns {critical, emailed, recipients}. Never raises."""
    from api.integrations import resend
    from api.services.redis_client import get_redis
    now = now or datetime.now(timezone.utc)
    result = {"critical": [], "emailed": [], "recipients": []}
    try:
        items = await compute_alerts(db)
    except Exception:
        logger.exception("alert evaluation failed")
        return result
    critical = [a for a in items if a.severity == "critical"]
    result["critical"] = [a.name for a in critical]
    if not critical:
        return result
    fresh = []
    for a in critical:
        try:
            if await get_redis().set(NOTIFIED_KEY.format(name=a.name), now.isoformat(), nx=True,
                                     ex=ALERT_EMAIL_REPEAT_HOURS * 3600):
                fresh.append(a)
        except Exception as exc:
            logger.error("alert de-duplication unavailable (%s); emailing anyway", exc)
            fresh.append(a)
    if not fresh:
        return result
    recipients = await admin_recipients(db)
    result["recipients"] = recipients
    warnings = [a for a in items if a.severity == "warning"]
    lines = [f"CRITICAL {a.name} = {a.value}: {a.detail}" for a in fresh]
    others = [f"critical (already emailed) {a.name} = {a.value}" for a in critical if a not in fresh]
    others += [f"warning {a.name} = {a.value}: {a.detail}" for a in warnings]
    text = (f"{settings.app_name or 'AutoBuy'} admin alerts at {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            + "\n".join(lines) + ("\n\nAlso open:\n" + "\n".join(others) if others else "")
            + f"\n\nSee the Alerts tab of the admin console. This message repeats at most every "
              f"{ALERT_EMAIL_REPEAT_HOURS} hours per condition.")
    html = "<pre>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    subject = f"[{settings.app_name or 'AutoBuy'}] CRITICAL: " + ", ".join(a.name for a in fresh)
    logger.critical("ADMIN ALERT EMAIL: %s -> %s", subject, recipients)
    for to in recipients:
        try:
            await resend.send_email(to, subject, html, text_content=text, template="admin_alert")
            result["emailed"].append(to)
        except Exception as exc:
            logger.error("admin alert email not delivered to %s: %s", to, exc)
    return result
