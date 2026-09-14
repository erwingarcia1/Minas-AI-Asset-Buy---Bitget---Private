"""Purchase emails are queued, retried with backoff, and never sent inline: a
hanging provider cannot delay a scheduler run, a transient outage does not
lose emails, and a permanent failure is recorded where an admin can see it."""
import asyncio
import json
import logging
import time

import pytest
from sqlalchemy import select

from api.integrations import resend
from api.models import Notification
from api.services import email_outbox
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for

T0 = 1_700_000_000.0   # fixed clock (in the past) for deterministic backoff


@pytest.fixture(autouse=True)
async def clean_outbox():
    from api.services.redis_client import get_redis
    r = get_redis()
    keys = [email_outbox.OUTBOX_KEY, email_outbox.DEAD_LETTER_KEY]
    keys += [k async for k in r.scan_iter("autobuy:email:failures:*")] + [k async for k in r.scan_iter("autobuy:email:failure-alert:*")]
    if keys:
        await r.delete(*keys)
    yield
    keys = [email_outbox.OUTBOX_KEY, email_outbox.DEAD_LETTER_KEY]
    keys += [k async for k in r.scan_iter("autobuy:email:failures:*")] + [k async for k in r.scan_iter("autobuy:email:failure-alert:*")]
    await r.delete(*keys)


@pytest.fixture
def hanging_provider(monkeypatch):
    calls = []

    async def hang(to_email, subject, html_content, **kw):
        calls.append(subject)
        await asyncio.sleep(30)
        return "never"
    monkeypatch.setattr(resend, "send_email", hang)
    return calls


@pytest.fixture
def working_provider(monkeypatch):
    box = []

    async def ok(to_email, subject, html_content, **kw):
        box.append({"to": to_email, "subject": subject, "text": kw.get("text_content"), "template": kw.get("template")})
        return "email-1"
    monkeypatch.setattr(resend, "send_email", ok)
    return box


class FlakyProvider:
    """Fails the first `failures` calls, then works."""

    def __init__(self, monkeypatch, failures):
        self.failures, self.calls = failures, []

        async def send(to_email, subject, html_content, **kw):
            self.calls.append(subject)
            if len(self.calls) <= self.failures:
                raise resend.ResendError("Resend unreachable: [Errno -3] Temporary failure in name resolution")
            return "email-ok"
        monkeypatch.setattr(resend, "send_email", send)


async def queued():
    return await email_outbox.pending(50)


# --------------------------------------------------------------------------- the run is never delayed

async def test_a_hanging_email_provider_does_not_delay_the_run(db, user, binance_key, hanging_provider):
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    started = time.perf_counter()
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    elapsed = time.perf_counter() - started
    assert counts["executed"] == 1 and elapsed < 5, f"run took {elapsed:.1f}s; emails must not be sent inline"
    assert hanging_provider == [], "the provider was never called during the run"
    assert await email_outbox.length() >= 1, "the confirmation (and campaign email) is queued instead"
    (buy,) = await tx_for(db, p)
    assert buy.status == "success"


async def test_drain_gives_up_on_a_hanging_provider_within_its_budget_and_keeps_the_message(db, user, binance_key, hanging_provider):
    await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    started = time.perf_counter()
    counts = await email_outbox.drain(budget_seconds=1.0, per_email_timeout=0.3)
    assert time.perf_counter() - started < 3
    assert counts["sent"] == 0 and counts["retried"] >= 1 and counts["remaining"] == counts["retried"], "kept for a later attempt, not dropped"
    for m in await queued():
        assert m["attempts"] == 1 and "timeout" in m["last_error"] and m["next_attempt_at"] > time.time() + 20


async def test_scheduler_run_drains_the_outbox_after_the_purchases_are_committed(db, user, binance_key, working_provider, monkeypatch):
    order = []

    async def fake_execute(session, **kw):
        order.append("executed")
        await email_outbox.enqueue(user.email, "queued during the run", "<p>x</p>", "x", template="purchase")
        return {"executed": 1, "failed": 0, "skipped": 0}
    monkeypatch.setattr(ps, "execute_due_purchases", fake_execute)
    real_drain = email_outbox.drain

    async def spy_drain(*a, **kw):
        order.append("drained")
        return await real_drain(*a, **kw)
    monkeypatch.setattr(email_outbox, "drain", spy_drain)
    counts = await ps._run_once()
    assert counts["executed"] == 1 and order == ["executed", "drained"]
    assert [m["subject"] for m in working_provider] == ["queued during the run"]


# --------------------------------------------------------------------------- retries

async def test_transient_failure_is_retried_with_backoff_then_delivered(db, user, monkeypatch, caplog):
    flaky = FlakyProvider(monkeypatch, failures=1)
    await email_outbox.enqueue(user.email, "Bought BTC", "<p>x</p>", "x", template="purchase",
                               category="confirmation", user_id=str(user.id), now=T0)
    with caplog.at_level(logging.WARNING):
        first = await email_outbox.drain(now=T0)
    assert first == {"sent": 0, "retried": 1, "gave_up": 0, "remaining": 1}
    (m,) = await queued()
    assert m["attempts"] == 1 and m["next_attempt_at"] == T0 + 30 and "name resolution" in m["last_error"]
    assert any("attempt 1/4 failed, retrying in 30s" in r.getMessage() and user.email in r.getMessage() for r in caplog.records)
    # not due yet: nothing happens
    assert (await email_outbox.drain(now=T0 + 10))["sent"] == 0 and len(flaky.calls) == 1
    # due: delivered on the second attempt
    with caplog.at_level(logging.INFO):
        second = await email_outbox.drain(now=T0 + 31)
    assert second == {"sent": 1, "retried": 0, "gave_up": 0, "remaining": 0} and len(flaky.calls) == 2
    assert any("delivered on attempt 2" in r.getMessage() for r in caplog.records)
    assert await email_outbox.undelivered() == []
    rows = (await db.execute(select(Notification).where(Notification.user_id == user.id, Notification.template == "purchase"))).scalars().all()
    assert [n.status for n in rows] == ["sent"]


def test_backoff_schedule_spans_about_fifteen_minutes_and_failures_get_more_attempts():
    delays = [email_outbox.backoff_for(n) for n in range(1, email_outbox.max_attempts("failure"))]
    assert delays == [30, 60, 120, 240, 480] and sum(delays) == 930   # 15.5 min
    assert email_outbox.max_attempts("failure") == email_outbox.max_attempts("security") == 6
    assert email_outbox.max_attempts("confirmation") == 4 < email_outbox.max_attempts("failure")


async def test_permanent_failure_exhausts_retries_and_is_recorded_for_admins(db, user, monkeypatch, caplog):
    flaky = FlakyProvider(monkeypatch, failures=99)
    await email_outbox.enqueue(user.email, "✗ Purchase failed: BTC", "<p>x</p>", "x", template="purchase",
                               category="failure", user_id=str(user.id), now=T0)
    t = T0
    gave_up = None
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            counts = await email_outbox.drain(now=t)
            if counts["gave_up"]:
                gave_up = counts
                break
            (m,) = await queued()
            t = m["next_attempt_at"]
    assert gave_up == {"sent": 0, "retried": 0, "gave_up": 1, "remaining": 0}
    assert len(flaky.calls) == 6, "failure emails get six attempts"
    assert t - T0 == pytest.approx(930), "spread over ~15 minutes"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("NOT delivered after 6 attempt(s)" in r.getMessage() and user.email in r.getMessage()
               and "template=purchase" in r.getMessage() and "category=failure" in r.getMessage() for r in errors)
    (dead,) = await email_outbox.undelivered()
    assert dead["to"] == user.email and dead["attempts"] == 6 and dead["template"] == "purchase" and "name resolution" in dead["last_error"]
    assert dead["gave_up_at"] == t and "html" not in dead
    rows = (await db.execute(select(Notification).where(Notification.user_id == user.id, Notification.template == "purchase"))).scalars().all()
    assert [n.status for n in rows] == ["failed"]
    assert await email_outbox.failures_this_hour(now=t) == 1


async def test_confirmations_give_up_after_four_attempts(user, monkeypatch):
    flaky = FlakyProvider(monkeypatch, failures=99)
    await email_outbox.enqueue(user.email, "Bought BTC", "<p>x</p>", "x", template="purchase", category="confirmation", now=T0)
    t = T0
    for _ in range(10):
        if (await email_outbox.drain(now=t))["gave_up"]:
            break
        (m,) = await queued()
        t = m["next_attempt_at"]
    assert len(flaky.calls) == 4 and t - T0 == pytest.approx(30 + 60 + 120)


async def test_admin_sees_undelivered_and_pending_emails_and_the_hourly_alert(db, user, client, monkeypatch):
    import uuid
    from api.models import User
    from api.routes.admin import require_admin  # noqa: F401
    from tests.api.conftest import auth_headers
    admin = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(admin)
    await db.commit()
    slack = []

    async def fake_slack(text):
        slack.append(text)
        return True
    from api.services import slack_service
    monkeypatch.setattr(slack_service, "send_slack", fake_slack)
    monkeypatch.setattr(email_outbox.settings, "email_failure_alert_threshold", 2)
    FlakyProvider(monkeypatch, failures=99)
    # a clock one minute into the current UTC hour: the give-ups (3.5 min later) land in the same hourly bucket
    now = time.time()
    base = (now // 3600) * 3600 + 60
    for i in range(3):
        await email_outbox.enqueue(f"u{i}@example.com", f"failed {i}", "<p>x</p>", "x", template="purchase", category="confirmation", now=base)
    # exhaust all three quickly by draining at their due times
    t = base
    for _ in range(12):
        await email_outbox.drain(now=t)
        pend = await queued()
        if not pend:
            break
        t = max(m["next_attempt_at"] for m in pend)
    await email_outbox.enqueue("later@example.com", "still trying", "<p>x</p>", "x", template="purchase", now=now)
    try:
        c = client
        H = auth_headers(admin)
        view = (await c.get("/api/admin/emails/undelivered", headers=H)).json()
        assert view["failures_this_hour"] == 3 and view["alert_threshold"] == 2
        assert sorted(u["to"] for u in view["undelivered"]) == ["u0@example.com", "u1@example.com", "u2@example.com"]
        assert view["queued"] == 1 and view["pending"][0]["to"] == "later@example.com" and "html" not in view["pending"][0]
        alerts = {a["name"]: a for a in (await c.get("/api/admin/alerts", headers=H)).json()}
        assert alerts["email_delivery_failures"]["severity"] == "critical" and alerts["email_delivery_failures"]["value"] == "3"
        assert len(slack) == 1 and "3 emails could not be delivered" in slack[0], "one Slack notice per hour"
    finally:
        await db.delete(admin)
        await db.commit()


# --------------------------------------------------------------------------- edge cases

async def test_queued_emails_still_drain_while_the_schedulers_are_paused(db, user, binance_key, working_provider):
    """Pausing stops trading, not notifications."""
    from api.services import scheduler_state
    await scheduler_state.pause(by="admin-test")
    try:
        await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
        counts = await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
        assert counts["executed"] == 0, "trading is paused"
        await email_outbox.enqueue(user.email, "queued while paused", "<p>x</p>", "x", template="purchase", now=time.time() - 5)
        assert (await email_outbox.drain())["sent"] == 1
        await email_outbox.enqueue(user.email, "queued while paused 2", "<p>x</p>", "x", template="purchase", now=time.time() - 4)
        await ps._run_once()   # the scheduler's run also drains, even though the run itself is skipped
        assert [m["subject"] for m in working_provider] == ["queued while paused", "queued while paused 2"]
    finally:
        await scheduler_state.resume()


async def test_redis_down_logs_error_with_recipient_and_template_and_leaves_the_purchase_alone(db, user, binance_key, monkeypatch, caplog):
    def no_redis():
        raise ConnectionError("redis refused")
    monkeypatch.setattr(email_outbox, "_redis", no_redis)

    async def provider_down(*a, **kw):
        raise resend.ResendError("Resend unreachable: boom")
    monkeypatch.setattr(resend, "send_email", provider_down)
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    with caplog.at_level(logging.ERROR):
        counts = await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    assert counts["executed"] == 1
    await db.refresh(p)
    (buy,) = await tx_for(db, p)
    assert p.status == "active" and p.failure_count == 0 and buy.status == "success"
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("email outbox unavailable" in m and user.email in m and "template=purchase" in m for m in errors)
    assert any("email lost" in m and user.email in m and "template=purchase" in m for m in errors)
    assert await email_outbox.drain() == {"sent": 0, "retried": 0, "gave_up": 0, "remaining": 0}


async def test_unreadable_messages_are_dropped_with_a_log_line(working_provider, caplog):
    from api.services.redis_client import get_redis
    await get_redis().zadd(email_outbox.OUTBOX_KEY, {"{not json": T0})
    await email_outbox.enqueue("a@example.com", "fine", "<p>x</p>", "x", template="purchase", now=T0)
    with caplog.at_level(logging.ERROR):
        counts = await email_outbox.drain(now=T0)
    assert counts["sent"] == 1 and counts["remaining"] == 0
    assert any("unreadable" in r.getMessage() for r in caplog.records)


async def test_outbox_is_capped_and_trimming_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(email_outbox, "MAX_QUEUE_LENGTH", 3)
    with caplog.at_level(logging.ERROR):
        for i in range(5):
            assert await email_outbox.enqueue("a@example.com", f"m{i}", "<p>x</p>", "x", template="purchase", now=T0 + i)
    assert await email_outbox.length() == 3
    assert [m["subject"] for m in await queued()] == ["m2", "m3", "m4"], "newest kept, earliest-due dropped"
    trims = [r.getMessage() for r in caplog.records if "over its cap" in r.getMessage()]
    assert len(trims) == 2 and "1 earliest-due message(s) dropped" in trims[0] and "template=purchase" in trims[0]


async def test_outbox_unavailable_falls_back_to_one_short_inline_attempt(working_provider, monkeypatch):
    from api.services.email_service import email_service

    async def no_redis(*a, **kw):
        return False
    monkeypatch.setattr(email_outbox, "enqueue", no_redis)
    assert await email_service.queue("a@example.com", "fallback", "<p>f</p>", "f", template="purchase") is True
    assert [m["subject"] for m in working_provider] == ["fallback"]


async def test_purchase_failure_emails_are_queued_as_failure_category(db, user, binance_key):
    from tests.fake_gateway import BuyFails
    await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=BuyFails(), dry_run=True)
    (m,) = await queued()
    assert m["category"] == "failure" and m["user_id"] == str(user.id) and "Purchase failed" in m["subject"]


def test_scheduler_has_the_outbox_job():
    sched = ps.build_scheduler()
    job = sched.get_job("drain_email_outbox")
    assert job is not None and job.func is ps.drain_email_outbox and job.max_instances == 1
