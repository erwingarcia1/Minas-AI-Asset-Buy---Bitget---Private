import uuid

import pytest

from api.services import purchase_service as ps_mod
from api.services.binance_client import BinanceClientError
from api.services.purchase_service import PurchaseNotFound, PurchaseService


class FakeBinance:
    """Stands in for api.services.binance_client.BinanceClient."""
    last = None

    def __init__(self, api_key, api_secret):
        FakeBinance.last = self
        self.api_key = api_key
        self.calls = []

    def execute_order(self, symbol, usdt_amount, dry_run=True, client_oid=None):
        self.calls.append((symbol, usdt_amount, dry_run, client_oid))
        return {"order_id": "DRY-fake" if dry_run else "123456",
                "amount_crypto": usdt_amount / 50000.0,
                "exchange_rate": 50000.0,
                "cost_usdt": usdt_amount,
                "dry_run": dry_run}


class FailingBinance(FakeBinance):
    def execute_order(self, *a, **k):
        raise BinanceClientError("Invalid API-key, IP, or permissions", "AUTH")


@pytest.fixture
def fake_binance(monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FakeBinance)
    return FakeBinance


async def test_dry_run_creates_completed_transaction(db, user, purchase, binance_key, fake_binance):
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id, dry_run=True)
    assert tx.status == "completed"
    assert tx.binance_order_id.startswith("DRY-")
    assert tx.exchange_rate == 50000.0
    assert tx.amount_crypto == pytest.approx(0.002)
    assert tx.executed_at is not None and tx.executed_at.tzinfo is not None
    # symbol mapping: lowercase model value -> uppercase Binance symbol
    assert fake_binance.last.calls[0][0] == "BTCUSDT"
    assert fake_binance.last.calls[0][2] is True  # dry_run reached the client
    # decrypted key reached the client, never the ciphertext
    assert fake_binance.last.api_key == "test-api-key-000001"


async def test_missing_api_key_records_failed(db, user, purchase, fake_binance):
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "failed"
    assert "no Binance API key" in tx.error_message
    assert tx.binance_order_id is None


async def test_unverified_kyc_records_failed(db, user, purchase, binance_key, fake_binance):
    user.kyc_status = "pending"
    await db.commit()
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "failed"
    assert "KYC" in tx.error_message


async def test_binance_error_records_failed(db, user, purchase, binance_key, monkeypatch):
    monkeypatch.setattr(ps_mod, "BinanceClient", FailingBinance)
    tx = await PurchaseService(db).execute_purchase(user.id, purchase.id)
    assert tx.status == "failed"
    assert "Invalid API-key" in tx.error_message


async def test_cross_user_isolation(db, user, purchase, fake_binance):
    with pytest.raises(PurchaseNotFound):
        await PurchaseService(db).execute_purchase(uuid.uuid4(), purchase.id)


async def test_due_date_idempotency(db, user, purchase, binance_key, fake_binance):
    from api.models import utcnow
    due = utcnow()
    tx1 = await PurchaseService(db).execute_purchase(user.id, purchase.id, due_date=due)
    tx2 = await PurchaseService(db).execute_purchase(user.id, purchase.id, due_date=due)
    assert tx1.id == tx2.id, "second execution for the same due date must not buy again"
    assert len(fake_binance.last.calls) == 1
