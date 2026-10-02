import pytest

from api.services.email_service import EmailService, email_service


class CapturingEmail(EmailService):
    def __init__(self):
        super().__init__(api_key="test-key")
        self.sent = []

    async def _send(self, to, subject, html, text, template="generic"):
        self.sent.append({"to": to, "subject": subject, "text": text,
                          "template": template})
        return True

    async def queue(self, to, subject, html, text, template="generic", **kw):
        """Queued (outbox) mail is captured the same way as direct sends."""
        return await self._send(to, subject, html, text, template)


class ExplodingEmail(EmailService):
    async def _send(self, to, subject, html, text, template="generic"):
        raise RuntimeError("resend is down")

    async def queue(self, to, subject, html, text, template="generic", **kw):
        raise RuntimeError("resend is down")


@pytest.fixture
def capture(monkeypatch):
    import api.services.email_service as es_mod
    svc = CapturingEmail()
    monkeypatch.setattr(es_mod, "email_service", svc)
    return svc


async def _run(db, user, key, gateway, **purchase):
    from datetime import timedelta
    from api.models import utcnow
    from api.tasks import purchase_scheduler as ps
    from tests.api.conftest import make_autobuy_purchase
    await make_autobuy_purchase(db, user, key=key, next_execution=utcnow() - timedelta(minutes=1), **purchase)
    return await ps.execute_due_purchases(db, gateway=gateway, dry_run=True)


async def test_completed_purchase_email(db, user, binance_key, capture):
    from tests.test_purchase_events import RichGateway
    counts = await _run(db, user, binance_key, RichGateway(), purchase_type="dca", amount_usd=100)
    assert counts["executed"] == 1
    mails = [m for m in capture.sent if m["template"] == "purchase"]
    assert len(mails) == 1
    mail = mails[0]
    assert mail["to"] == user.email
    assert mail["subject"].startswith("[DRY RUN] ✓ Purchase complete") and "BTC" in mail["subject"]
    assert "50,000.00" in mail["text"] and "ORD-1" in mail["text"] and "held on the exchange" in mail["text"]


async def test_failed_purchase_email(db, user, binance_key, capture):
    """One email per failed cycle: nothing on attempts 1 and 2, a "Purchase missed"
    email when the third attempt fails, naming the next attempt time."""
    from datetime import timedelta
    from api.tasks import purchase_scheduler as ps
    from tests.conftest_autobuy import make_purchase
    from tests.test_purchase_events import BuyFails
    from tests.test_purchase_scheduler import NOW
    await make_purchase(db, user, binance_key, purchase_type="dca", frequency="daily", next_execution=NOW)
    for i in range(2):
        counts = await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=5 * i), gateway=BuyFails(), dry_run=True)
        assert counts["failed"] == 1
    assert [m for m in capture.sent if m["template"] == "purchase"] == [], "attempts 1 and 2 send nothing"
    await ps.execute_due_purchases(db, now=NOW + timedelta(minutes=10), gateway=BuyFails(), dry_run=True)
    mail = [m for m in capture.sent if m["template"] == "purchase"]
    assert len(mail) == 1 and mail[0]["subject"].startswith("[DRY RUN] ✗ Purchase missed")
    assert "MIN_NOTIONAL" in mail[0]["text"] and "tried 3 times" in mail[0]["text"] and "next attempt is at" in mail[0]["text"]


async def test_email_failure_never_breaks_purchase(db, user, binance_key, monkeypatch):
    import api.services.email_service as es_mod
    from tests.test_purchase_events import RichGateway
    monkeypatch.setattr(es_mod, "email_service", ExplodingEmail())
    counts = await _run(db, user, binance_key, RichGateway())
    assert counts["executed"] == 1, "email outage must not fail the purchase"


async def test_kyc_emails():
    svc = CapturingEmail()
    assert await svc.send_kyc_notification("a@b.c", "verified")
    assert svc.sent[-1]["subject"] == "✓ Identity verified"
    assert await svc.send_kyc_notification("a@b.c", "rejected")
    assert svc.sent[-1]["subject"].startswith("✗ Identity verification rejected")
    assert not await svc.send_kyc_notification("a@b.c", "pending")
    assert len(svc.sent) == 2


async def test_unconfigured_key_is_noop():
    svc = EmailService(api_key="")
    svc.api_key = ""
    assert await svc.send_kyc_notification("a@b.c", "verified") is False
