import pytest

from api.services import purchase_service as ps_mod
from api.services.email_service import EmailService, email_service
from api.services.purchase_service import PurchaseService
from tests.api.test_purchase_service import FakeBinance


class CapturingEmail(EmailService):
    def __init__(self):
        super().__init__(api_key="test-key")
        self.sent = []

    async def _send(self, to, subject, html, text):
        self.sent.append({"to": to, "subject": subject, "text": text})
        return True


class ExplodingEmail(EmailService):
    async def _send(self, to, subject, html, text):
        raise RuntimeError("resend is down")


@pytest.fixture
def capture(monkeypatch):
    svc = CapturingEmail()
    monkeypatch.setattr(ps_mod, "email_service", svc)
    return svc


async def test_completed_purchase_email(db, user, purchase, binance_key, monkeypatch, capture):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed"
    assert len(capture.sent) == 1
    mail = capture.sent[0]
    assert mail["to"] == user.email
    assert mail["subject"].startswith("✓ Purchase complete")
    assert "BTC" in mail["subject"] and "50000.00" in mail["text"]
    assert tx.binance_order_id in mail["text"]


async def test_failed_purchase_email(db, user, purchase, capture):
    # no API key on file -> failed tx -> failure email
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "failed"
    mail = capture.sent[0]
    assert mail["subject"].startswith("✗ Purchase failed")
    assert "no Binance API key" in mail["text"]


async def test_email_failure_never_breaks_purchase(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    monkeypatch.setattr(ps_mod, "email_service", ExplodingEmail())
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "completed", "email outage must not fail the purchase"


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
