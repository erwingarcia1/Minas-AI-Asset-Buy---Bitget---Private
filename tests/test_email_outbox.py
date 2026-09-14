"""Purchase emails are queued, not sent inline: a hanging provider cannot
delay a scheduler run, and a failed email never touches a purchase."""
import asyncio
import time

import pytest

from api.integrations import resend
from api.services import email_outbox
from api.tasks import purchase_scheduler as ps
from tests.conftest_autobuy import binance_key, client, db, make_key, make_purchase, other_user, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW, tx_for


@pytest.fixture(autouse=True)
async def clean_outbox():
    from api.services.redis_client import get_redis
    await get_redis().delete(email_outbox.OUTBOX_KEY)
    yield
    await get_redis().delete(email_outbox.OUTBOX_KEY)


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
        box.append({"to": to_email, "subject": subject, "text": kw.get("text_content")})
        return "email-1"
    monkeypatch.setattr(resend, "send_email", ok)
    return box


async def test_a_hanging_email_provider_does_not_delay_the_run(db, user, binance_key, hanging_provider):
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    started = time.perf_counter()
    counts = await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    elapsed = time.perf_counter() - started
    assert counts["executed"] == 1 and elapsed < 5, f"run took {elapsed:.1f}s; emails must not be sent inline"
    assert hanging_provider == [], "the provider was never called during the run"
    assert await email_outbox.length() >= 1, "the confirmation is queued instead"
    (buy,) = await tx_for(db, p)
    assert buy.status == "success"


async def test_drain_gives_up_on_a_hanging_provider_within_its_budget(db, user, binance_key, hanging_provider):
    await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    queued = await email_outbox.length()
    started = time.perf_counter()
    counts = await email_outbox.drain(budget_seconds=1.0, per_email_timeout=0.3)
    assert time.perf_counter() - started < 3
    assert counts["timed_out"] >= 1 and counts["sent"] == 0 and counts["remaining"] <= queued - 1
    assert len(hanging_provider) >= 1, "the send was attempted, then dropped with a log line"


async def test_drain_sends_queued_emails_in_order(working_provider):
    assert await email_outbox.enqueue("a@example.com", "first", "<p>1</p>", "1", template="purchase")
    assert await email_outbox.enqueue("a@example.com", "second", "<p>2</p>", "2", template="purchase")
    counts = await email_outbox.drain()
    assert counts == {"sent": 2, "failed": 0, "timed_out": 0, "remaining": 0}
    assert [m["subject"] for m in working_provider] == ["first", "second"]
    assert await email_outbox.length() == 0
    assert await email_outbox.drain() == {"sent": 0, "failed": 0, "timed_out": 0, "remaining": 0}


async def test_scheduler_run_drains_the_outbox_after_the_purchases_are_committed(db, user, binance_key, working_provider, monkeypatch):
    """_run_once: execute, then send. The purchase outcome is written before any email goes out."""
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


async def test_failed_or_broken_emails_are_dropped_and_never_touch_the_purchase(db, user, binance_key, monkeypatch):
    async def broken(to_email, subject, html_content, **kw):
        raise resend.ResendError("Resend 500: boom", status=500)
    monkeypatch.setattr(resend, "send_email", broken)
    p = await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
    await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
    counts = await email_outbox.drain()
    assert counts["failed"] >= 1 and counts["remaining"] == 0, "dropped, not retried"
    await db.refresh(p)
    assert p.status == "active" and p.failure_count == 0
    (buy,) = await tx_for(db, p)
    assert buy.status == "success"


async def test_outbox_unavailable_falls_back_to_one_short_inline_attempt(working_provider, monkeypatch):
    from api.services.email_service import email_service

    async def no_redis(*a, **kw):
        return False
    monkeypatch.setattr(email_outbox, "enqueue", no_redis)
    assert await email_service.queue("a@example.com", "fallback", "<p>f</p>", "f", template="purchase") is True
    assert [m["subject"] for m in working_provider] == ["fallback"]


def test_scheduler_has_the_outbox_job():
    sched = ps.build_scheduler()
    job = sched.get_job("drain_email_outbox")
    assert job is not None and job.func is ps.drain_email_outbox and job.max_instances == 1


# --------------------------------------------------------------------------- edge cases

async def test_queued_emails_still_drain_while_the_schedulers_are_paused(db, user, binance_key, working_provider):
    """Pausing stops trading, not notifications."""
    from api.services import scheduler_state
    await scheduler_state.pause(by="admin-test")
    try:
        await make_purchase(db, user, binance_key, purchase_type="dca", next_execution=NOW)
        counts = await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(), dry_run=True)
        assert counts["executed"] == 0, "trading is paused"
        await email_outbox.enqueue(user.email, "queued while paused", "<p>x</p>", "x", template="purchase")
        assert (await email_outbox.drain())["sent"] == 1
        await email_outbox.enqueue(user.email, "queued while paused 2", "<p>x</p>", "x", template="purchase")
        await ps._run_once()   # the scheduler's run also drains, even though the run itself is skipped
        assert [m["subject"] for m in working_provider] == ["queued while paused", "queued while paused 2"]
    finally:
        await scheduler_state.resume()


async def test_redis_down_logs_error_with_recipient_and_template_and_leaves_the_purchase_alone(db, user, binance_key, monkeypatch, caplog):
    import logging

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
    # the drain itself copes with Redis being gone
    assert await email_outbox.drain() == {"sent": 0, "failed": 0, "timed_out": 0, "remaining": 0}


async def test_dropped_emails_are_logged_with_recipient_and_template(working_provider, monkeypatch, caplog):
    import logging
    from api.services.redis_client import get_redis
    await get_redis().rpush(email_outbox.OUTBOX_KEY, "{not json")
    await email_outbox.enqueue("a@example.com", "will fail", "<p>x</p>", "x", template="purchase")

    async def reject(*a, **kw):
        raise resend.ResendError("Resend 422: bad address", status=422)
    monkeypatch.setattr(resend, "send_email", reject)
    with caplog.at_level(logging.ERROR):
        counts = await email_outbox.drain()
    assert counts["failed"] == 2 and counts["remaining"] == 0
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("unreadable" in m for m in errors)
    assert any("email dropped" in m and "a@example.com" in m and "template=purchase" in m for m in errors)


async def test_outbox_is_capped_and_trimming_is_logged(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(email_outbox, "MAX_QUEUE_LENGTH", 3)
    with caplog.at_level(logging.ERROR):
        for i in range(5):
            assert await email_outbox.enqueue("a@example.com", f"m{i}", "<p>x</p>", "x", template="purchase")
    assert await email_outbox.length() == 3
    from api.services.redis_client import get_redis
    import json
    kept = [json.loads(x)["subject"] for x in await get_redis().lrange(email_outbox.OUTBOX_KEY, 0, -1)]
    assert kept == ["m2", "m3", "m4"], "newest kept, oldest dropped"
    trims = [r.getMessage() for r in caplog.records if "over its cap" in r.getMessage()]
    assert len(trims) == 2 and "1 oldest message(s) dropped" in trims[0] and "template=purchase" in trims[0]
