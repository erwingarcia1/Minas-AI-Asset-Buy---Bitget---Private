"""Meme coins (DOGE, SHIB, PEPE) are accumulate-only by category: bought and held
on the exchange, never withdraw mode, whatever the allowlist says; the registry
serves categories; prices and amounts format for humans and for machines."""
import csv
import io
import re
import uuid
import zlib

import httpx
import pytest
from sqlalchemy import select

from api.config import settings
from api.models import Purchase, PurchaseTransaction, User
from api.services import bitget_market, history, purchase_events, report_generator, telegram
from api.tasks import purchase_scheduler as ps
from api.utils import exchange_networks as en
from tests.api.conftest import make_history
from tests.api.test_email_service import CapturingEmail
from tests.conftest_autobuy import auth, bitget_key, db, make_key, make_purchase, user  # noqa: F401
from tests.fake_gateway import FakeGateway
from tests.test_purchase_scheduler import NOW
from tests.test_withdrawal_ops import reload

BTC_ADDR = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
PEPE_PRICE = 0.000004181
# Bitget public symbol data as read on 2026-10-02 (normalised fields the app keeps)
RECORDED = {
    "DOGE": {"symbol": "DOGEUSDT", "status": "online", "minTradeUSDT": "1", "quantityPrecision": "4", "pricePrecision": "5", "quotePrecision": "9", "takerFeeRate": "0.001"},
    "SHIB": {"symbol": "SHIBUSDT", "status": "online", "minTradeUSDT": "1", "quantityPrecision": "4", "pricePrecision": "9", "quotePrecision": "13", "takerFeeRate": "0.001"},
    "PEPE": {"symbol": "PEPEUSDT", "status": "online", "minTradeUSDT": "1", "quantityPrecision": "0", "pricePrecision": "9", "quotePrecision": "9", "takerFeeRate": "0.001"},
    "BTC": {"symbol": "BTCUSDT", "status": "online", "minTradeUSDT": "1", "quantityPrecision": "6", "pricePrecision": "2", "quotePrecision": "8", "takerFeeRate": "0.001"},
}


def _inflate(blob: bytes) -> bytes:
    """reportlab writes content streams as ASCII85 + Flate."""
    import base64
    d = blob.strip()
    d = d[:-2] if d.endswith(b"~>") else d
    try:
        return zlib.decompress(base64.a85decode(d, adobe=False))
    except Exception:
        return b""


def api():
    from api.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def everyone_allowed(monkeypatch):
    """The strongest possible withdraw permission: global switch on AND the owner allowlisted."""
    monkeypatch.setattr(settings, "withdrawals_enabled", "1")


@pytest.fixture
async def admin(db):
    u = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", role="admin", kyc_status="verified")
    db.add(u); await db.commit()
    yield u
    fresh = (await db.execute(select(User).where(User.id == u.id))).scalar_one_or_none()
    if fresh is not None:
        await db.delete(fresh); await db.commit()


def test_registry_categories_and_policy():
    assert [c["category"] for c in en.categories_with_coins()] == ["main", "stable", "meme"]
    by = {c["category"]: c for c in en.categories_with_coins()}
    assert [x["symbol"] for x in by["main"]["coins"]] == ["BTC", "ETH", "SOL", "XRP", "XLM"]
    assert [x["symbol"] for x in by["stable"]["coins"]] == ["USDC"]
    assert [(x["symbol"], x["name"]) for x in by["meme"]["coins"]] == [("DOGE", "Dogecoin"), ("SHIB", "Shiba Inu"), ("PEPE", "Pepe")]
    assert by["meme"]["accumulate_only"] is True and by["main"]["accumulate_only"] is False and by["stable"]["risk_note"] is None
    assert by["meme"]["risk_note"] == "Meme coins can move very sharply in price. Only buy what you can afford to lose."
    for label in (c["label"] for c in en.categories_with_coins()):
        assert not re.search(r"best|top|recommend|safe|popular", label, re.I), "labels describe, never rank"
    assert en.is_withdrawable("BTC") and not en.is_withdrawable("PEPE") and not en.is_withdrawable("nope")
    assert en.ACCUMULATE_ONLY == {"DOGE", "SHIB", "PEPE"}
    assert "PEPE" not in en.CRYPTO_CHAINS, "no address family: nothing to validate, nothing to withdraw to"


async def test_every_withdraw_path_is_refused_for_a_meme_coin_even_when_everyone_is_allowed(db, user, bitget_key, admin, everyone_allowed, monkeypatch):
    from api.routes.admin import purchases as admin_routes
    monkeypatch.setattr(settings, "withdraw_allowed_emails_csv", f"{user.email},{admin.email}")
    H, A = auth(user), auth(admin)
    msg = "PEPE is bought and held on Bitget only; it can't be sent to a wallet."
    base = {"crypto_symbol": "PEPE", "amount_usd": 10, "frequency": "weekly", "exchange_account_id": str(bitget_key.id)}
    async with api() as c:
        # 1. create in withdraw mode
        r = await c.post("/api/v2/purchases", json={**base, "purchase_type": "autobuy", "wallet_address": BTC_ADDR, "blockchain_network": "BTC"}, headers=H)
        assert r.status_code == 403 and r.json()["detail"]["details"] == {"message": msg, "coin": "PEPE", "accumulate_only": True}
        # 2. create accumulate with a wallet on file (exists only for a later switch)
        r = await c.post("/api/v2/purchases", json={**base, "purchase_type": "dca", "wallet_address": BTC_ADDR, "blockchain_network": "BTC"}, headers=H)
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        # plain accumulate works
        r = await c.post("/api/v2/purchases", json={**base, "purchase_type": "dca"}, headers=H)
        assert r.status_code == 201, r.text
        pid = r.json()["purchase_id"]
        row = (await c.get(f"/api/v2/purchases/{pid}", headers=H)).json()
        assert row["purchase_type"] == "dca" and row["wallet_address"] is None and row["blockchain_network"] is None
        # 3. switch to withdraw   4. set a wallet
        r = await c.put(f"/api/v2/purchases/{pid}", json={"purchase_type": "autobuy", "wallet_address": BTC_ADDR, "confirm_wallet_address": BTC_ADDR, "blockchain_network": "BTC"}, headers=H)
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        r = await c.put(f"/api/v2/purchases/{pid}", json={"wallet_address": BTC_ADDR, "blockchain_network": "BTC"}, headers=H)
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        # a BTC purchase by the same (allowed) owner still can: the check is per coin
        r = await c.post("/api/v2/purchases", json={**base, "crypto_symbol": "BTC", "amount_usd": 60, "purchase_type": "autobuy", "wallet_address": BTC_ADDR, "blockchain_network": "BTC"}, headers=H)
        assert r.status_code == 201, r.text
        # 5. retry-withdrawal   6. admin force-retry   (rows that could only exist from before the rule)
        legacy = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", purchase_type="autobuy", status="withdrawal_failed", next_execution=None)
        r = await c.post(f"/api/v2/purchases/{legacy.id}/retry-withdrawal", headers=H)
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        legacy.status = "failed"; legacy.failure_count = 3; await db.commit()
        r = await c.post(f"/api/admin/purchases/{legacy.id}/force-retry", headers=A)
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg, r.text
        assert (await reload(db, legacy.id)).status == "failed"
        # 7. admin resolve, both resolutions; keeping the coins on the exchange stays possible
        parked = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", purchase_type="autobuy", amount_usd=10, next_execution=NOW)
        await ps.execute_due_purchases(db, now=NOW, gateway=FakeGateway(fill_factor=0.0), dry_run=False)
        parked = await reload(db, parked.id); assert parked.status == "needs_review"
        monkeypatch.setattr(admin_routes, "_gateway", lambda: FakeGateway(lookup_state="filled"))
        await c.get(f"/api/admin/purchases/{parked.id}/review", headers=A)
        r = await c.post(f"/api/admin/purchases/{parked.id}/resolve", headers=A, json={"action": "record_fill", "note": "filled on Bitget"})
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        monkeypatch.setattr(admin_routes, "_gateway", lambda: FakeGateway(lookup_state="not_found"))
        r = await c.post(f"/api/admin/purchases/{parked.id}/resolve", headers=A, json={"action": "mark_not_sent", "note": "never reached Bitget"})
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        monkeypatch.setattr(admin_routes, "_gateway", lambda: FakeGateway(lookup_state="filled"))
        r = await c.post(f"/api/admin/purchases/{parked.id}/resolve", headers=A, json={"action": "record_fill", "note": "filled on Bitget"})
        assert r.status_code == 403 and r.json()["detail"]["details"]["message"] == msg
        r = await c.post(f"/api/admin/purchases/{parked.id}/resolve", headers=A, json={"action": "record_fill", "keep_on_exchange": True, "note": "filled on Bitget"})
        assert r.status_code == 200 and r.json()["purchase_type"] == "dca" and r.json()["status"] == "active"


class PepeGateway(FakeGateway):
    PRICE = PEPE_PRICE

    def _fill_for(self, purchase, order_id):
        fill = super()._fill_for(purchase, order_id)
        whole = float(int(purchase.amount_usd / self.PRICE))        # Bitget fills PEPE in whole coins
        fill.update(amount_crypto=whole, fee=whole * self.FEE_RATE, cost_usdt=round(whole * self.PRICE, 6))
        return fill


async def test_ten_usdt_pepe_buy_end_to_end(db, user, bitget_key, monkeypatch):
    p = await make_purchase(db, user, bitget_key, crypto_symbol="PEPE", purchase_type="dca", amount_usd=10.0, wallet_address=None, blockchain_network=None, next_execution=NOW)
    gw = PepeGateway()
    await ps.execute_due_purchases(db, now=NOW, gateway=gw, dry_run=False)
    p = await reload(db, p.id)
    tx = (await db.execute(select(PurchaseTransaction).where(PurchaseTransaction.purchase_id == p.id))).scalar_one()
    assert tx.status == "success" and tx.amount_executed == 2391772.0 and tx.fill_price == PEPE_PRICE and tx.fee == pytest.approx(2391.772)
    assert p.status == "active" and gw.withdrawals == [], "accumulate: held on the exchange, nothing withdrawn"
    pos = {x.crypto: x for x in await history.positions(db, user.id)}["PEPE"]
    assert pos.acquired == pytest.approx(2391772.0 - 2391.772) and pos.cost_usd == pytest.approx(tx.cost_usd)
    # human text: amount with separators and 2 decimals, price with 4 significant digits
    event = purchase_events.PurchaseEvent(type="buy_executed", user=user, purchase=p, tx=tx, dry_run=False, exchange="bitget")
    mail = CapturingEmail()
    await mail.send_purchase_event_email(user.email, event)
    assert "2,391,772 PEPE" in mail.sent[0]["subject"] and "at 0.000004181" in mail.sent[0]["text"]
    text, _ = telegram.format_purchase_event(event)
    assert "→ 2,391,772 PEPE @ 0.000004181 USDT" in text and "held on the exchange" in text
    async with api() as c:
        row = (await c.get(f"/api/v2/purchases/{p.id}", headers=auth(user))).json()
        assert row["latest_transaction"]["amount_executed"] == 2391772.0, "the API keeps raw numbers"
        assert row["latest_transaction"]["fill_price"] == PEPE_PRICE


async def test_csv_is_plain_and_round_trips_while_pdf_formats_for_humans(db, user):
    from datetime import datetime, timezone
    year = datetime.now(timezone.utc).year
    await make_history(db, user, crypto="PEPE", amount_crypto=2391772.0, cost_usd=10.0, purchase_type="dca", fill_price=PEPE_PRICE, fee=2391.772, fee_currency="PEPE")
    await make_history(db, user, crypto="SHIB", amount_crypto=1778330.9, cost_usd=10.0, purchase_type="dca", fill_price=0.000005621)
    await make_history(db, user, crypto="BTC", amount_crypto=0.001, cost_usd=84.48736, purchase_type="dca", fill_price=84487.36)
    summary = await report_generator.tax_summary(db, user.id, year)
    rows = {r["crypto"]: r for r in csv.DictReader(io.StringIO(report_generator.tax_summary_csv(summary)))}
    assert rows["PEPE"]["quantity"] == "2391772" and rows["PEPE"]["fill_price_usd"] == "0.000004181" and rows["PEPE"]["net_quantity"] == "2389380.228"
    assert rows["SHIB"]["fill_price_usd"] == "0.000005621" and rows["BTC"]["fill_price_usd"] == "84487.36" and rows["BTC"]["quantity"] == "0.001"
    for r in rows.values():
        for col in ("quantity", "net_quantity", "fill_price_usd", "usd_amount", "fee"):
            assert "," not in r[col], (col, r[col])
    assert float(rows["PEPE"]["quantity"]) == 2391772.0 and float(rows["PEPE"]["fill_price_usd"]) == PEPE_PRICE
    assert float(rows["SHIB"]["fill_price_usd"]) == 0.000005621 and float(rows["BTC"]["fill_price_usd"]) == 84487.36
    pdf = report_generator.tax_summary_pdf(summary, user.email)
    text = pdf + b"".join(_inflate(m) for m in re.findall(rb"stream\r?\n(.*?)endstream", pdf, re.S))
    # the PDF table: gross amount with separators, average price at 4 significant digits
    assert b"(2,391,772)" in text and b"(0.000004181)" in text and b"(84,487.36)" in text and b"(1,778,330.9)" in text, "PDF is for humans"
    assert summary["acquisitions"][1]["average_price_usdt"] == pytest.approx(10 / 2391772, rel=1e-9), "API keeps the raw average"


async def test_market_and_meta_carry_the_category(db, user, monkeypatch):
    async def fake_symbol(coin):
        return bitget_market.normalize_symbol(RECORDED[coin])
    async def fake_chains(coin):
        return []
    monkeypatch.setattr(bitget_market, "get_symbol", fake_symbol)
    monkeypatch.setattr(bitget_market, "get_chains", fake_chains)
    async with api() as c:
        pepe = (await c.get("/api/v2/market/coins/PEPE", headers=auth(user))).json()
        assert pepe["name"] == "Pepe" and pepe["category"] == "meme" and pepe["category_label"] == "Meme coins"
        assert pepe["accumulate_only"] is True and "sharply" in pepe["risk_note"] and pepe["quantity_precision"] == 0 and pepe["min_trade_usdt"] == 1.0
        btc = (await c.get("/api/v2/market/coins/BTC", headers=auth(user))).json()
        assert btc["category"] == "main" and btc["accumulate_only"] is False and btc["risk_note"] is None
        meta = (await c.get("/api/public/meta")).json()
        assert meta["coins"] == list(en.SUPPORTED_CRYPTOS) and meta["accumulate_only"] == ["DOGE", "PEPE", "SHIB"]
        assert [g["category"] for g in meta["coin_categories"]] == ["main", "stable", "meme"]
        assert meta["coin_categories"][2]["coins"][2] == {"symbol": "PEPE", "name": "Pepe"}


def test_bitget_symbol_check_passes_for_the_three_coins_on_recorded_data():
    for coin in ("DOGE", "SHIB", "PEPE"):
        sym = bitget_market.normalize_symbol(RECORDED[coin])
        assert sym["online"] and sym["min_trade_usdt"] == 1.0
        bitget_market.check_symbol(coin, 10.0, sym)   # no exception: 10 USDT clears the minimum
    assert bitget_market.normalize_symbol(RECORDED["PEPE"])["quantity_precision"] == 0


def test_formatters():
    assert history.fmt_amount(2389380.228) == "2,389,380.23" and history.fmt_amount(1000) == "1,000" and history.fmt_amount(999.99999999) == "999.99999999"
    assert history.fmt_amount(0.000196803) == "0.0001968" and history.fmt_amount(0.5) == "0.5" and history.fmt_amount(0) == "0"
    assert history.fmt_amount_plain(2389380.228) == "2389380.228" and history.fmt_amount_plain(1000) == "1000"
    assert history.fmt_price(84487.36) == "84,487.36" and history.fmt_price(0.000005621) == "0.000005621" and history.fmt_price(0.09179) == "0.09179"
    assert history.fmt_price(1.0) == "1.00" and history.fmt_price(2345.678) == "2,345.68" and history.fmt_price(0.2407) == "0.2407"
    assert history.fmt_price_plain(84487.36) == "84487.36" and history.fmt_price_plain(0.000004181) == "0.000004181"
    assert bitget_market.fmt_price(PEPE_PRICE, "PEPE") == "0.000004181" and bitget_market.fmt_price(None, "PEPE") == "market"
    assert "e" not in history.fmt_price(0.000000012) and history.fmt_amount(None) == ""
