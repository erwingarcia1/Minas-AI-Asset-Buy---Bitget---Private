from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from guardian.onboarding import OnboardingRejected, _bybit_read_only, onboard_account, verify_read_only
from tests.guardian.conftest import TEST_USER_ID


def query_api(read_only=1, permissions=None):
    """Bybit /v5/user/query-api shape (result wrapped), as CCXT returns it.
    The default permissions mirror the populated-scopes example in Bybit's own
    apikey-info docs for a readOnly:1 key - real keys ALWAYS carry scopes
    (they're what the key may read), so mocking empty arrays proves nothing."""
    if permissions is None:
        permissions = {
            "ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"],
            "Wallet": ["AccountTransfer", "SubMemberTransfer"], "Options": [],
            "Derivatives": ["DerivativesTrade"], "Exchange": ["ExchangeHistory"],
            "NFT": ["NFTQueryProductList"],
        }
    return {"result": {"readOnly": read_only, "permissions": permissions, "ips": ["1.2.3.4"]}}


def _bybit_client(read_only=1, permissions=None, balance_error=None):
    """A mock ExchangeClient mimicking BybitClient's normalized surface."""
    client = MagicMock()
    if balance_error is not None:
        client.fetch_balance.side_effect = balance_error
    else:
        client.fetch_balance.return_value = {"total": {"USDT": 500}}
    # BybitClient.fetch_account_info normalizes to {"data": {...}} with the
    # readOnly / permissions fields lifted out of result.
    result = query_api(read_only, permissions)["result"]
    data = dict(result)
    data.setdefault("authorities", data.get("permissions"))
    client.fetch_account_info.return_value = {"data": data}
    client.fetch_deposits.return_value = []
    client.fetch_withdrawals.return_value = []
    client.fetch_closed_orders.return_value = []
    return client


# --- unit: the fail-closed gate itself --------------------------------------

def test_read_only_flag_1_accepted():
    ok, _ = _bybit_read_only({"readOnly": 1, "permissions": {"Spot": [], "Wallet": []}})
    assert ok


def test_realistic_readonly_key_with_populated_scopes_accepted():
    # Regression: Bybit permission entries are SCOPES (what the key may READ),
    # not write grants. A real read-only key that can read balances/orders
    # always lists these - it must pass the gate, not be rejected.
    ok, reason = _bybit_read_only(query_api()["result"])
    assert ok, reason


def test_read_only_flag_0_rejected():
    ok, reason = _bybit_read_only({"readOnly": 0, "permissions": {"Spot": []}})
    assert not ok and "write access" in reason


def test_missing_flag_fails_closed():
    ok, reason = _bybit_read_only({"permissions": {}})
    assert not ok and "failing closed" in reason


def test_non_dict_fails_closed():
    ok, _ = _bybit_read_only("nope")
    assert not ok


def test_trade_scopes_on_readonly_key_are_accepted():
    # readOnly==1 is the platform-enforced write bar; SpotTrade here is the
    # scope that lets the key READ spot orders. Rejecting it would make every
    # usable read-only key fail onboarding.
    ok, _ = _bybit_read_only({"readOnly": 1, "permissions": {"Spot": ["SpotTrade"]}})
    assert ok


def test_withdraw_permission_rejected_even_if_flag_says_readonly():
    ok, reason = _bybit_read_only({"readOnly": 1, "permissions": {"Wallet": ["Withdraw"]}})
    assert not ok and "Withdraw" in reason


def test_readonly_history_permissions_accepted():
    ok, _ = _bybit_read_only(
        {"readOnly": 1, "permissions": {"Exchange": ["ExchangeHistory"], "NFT": ["NFTQueryProductList"]}}
    )
    assert ok


# --- integration: verify_read_only + onboard_account with bybit dispatch -----

def test_verify_read_only_accepts_readonly_bybit_key():
    ok, _ = verify_read_only(_bybit_client(read_only=1), exchange="bybit")
    assert ok


def test_verify_read_only_rejects_trade_bybit_key():
    ok, _ = verify_read_only(
        _bybit_client(read_only=0, permissions={"ContractTrade": ["Order", "Position"]}), exchange="bybit"
    )
    assert not ok


def test_onboard_bybit_read_only_persists_with_exchange_and_contact(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _bybit_client(read_only=1))
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="erwin-bybit", topology="independent",
        api_key="k", secret="s", passphrase="", exchange="bybit",
        alert_phone="+16477216549",
    )
    with db.cursor() as cur:
        cur.execute("SELECT exchange, user_id, alert_phone, encrypted_passphrase FROM guardian_accounts WHERE id = %s",
                    (result.account_id,))
        row = cur.fetchone()
    assert row["exchange"] == "bybit"
    assert row["user_id"] == TEST_USER_ID   # the owner's users.email is the alert destination
    assert row["alert_phone"] == "+16477216549"
    # passphrase stored as encrypted empty string, decrypts back to ""
    assert cipher.decrypt(row["encrypted_passphrase"]) == ""


def test_onboard_bybit_trade_key_rejected_and_persists_nothing(db, cipher, monkeypatch):
    monkeypatch.setattr(
        "guardian.onboarding.make_client",
        lambda *a, **k: _bybit_client(read_only=0, permissions={"Spot": ["SpotTrade"]}),
    )
    with pytest.raises(OnboardingRejected):
        onboard_account(db, cipher, user_id=TEST_USER_ID, label="bad-bybit", topology="independent",
            api_key="k", secret="s", passphrase="", exchange="bybit",
        )
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM guardian_accounts")
        assert cur.fetchone()["c"] == 0


def test_unsupported_exchange_rejected(db, cipher):
    with pytest.raises(ValueError):
        onboard_account(db, cipher, user_id=TEST_USER_ID, label="x", topology="independent",
            api_key="k", secret="s", passphrase="", exchange="kraken",
        )
