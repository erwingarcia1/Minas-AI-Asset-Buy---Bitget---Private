from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from guardian.exchange import BitgetClient, TamperSignal
from guardian.onboarding import OnboardingRejected, onboard_account, verify_read_only
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID


def _client_with(
    balance_result=None,
    balance_error=None,
    account_info_result=None,
    account_info_error=None,
    deposits=None,
    withdrawals=None,
    trades=None,
):
    """A BitgetClient with its network-touching methods replaced by mocks -
    never hits a live exchange.

    That sentence was false for two years. The five methods named below were
    mocked; `fetch_closed_orders` and `fetch_transfers` were not, and
    `seed_baseline` calls both - so every run of
    `test_onboard_account_persists_encrypted_on_success` made two real HTTP
    requests to Bitget with the key "key". Nothing failed, because both calls
    sit inside best-effort `except Exception` handlers that record a baseline
    gap and carry on. Found by the refusal counter added with #68.

    So the mocking is now a sweep over the whole read surface rather than a
    list somebody has to extend. A method added to `ExchangeClient` next year
    is mocked here the day it exists, which is the only version of this that
    stays true."""
    client = BitgetClient("key", "secret", "pass")
    for name in dir(client):
        if name.startswith("fetch_"):
            setattr(client, name, MagicMock(return_value=[]))
    client.fetch_balance = (
        MagicMock(side_effect=balance_error)
        if balance_error is not None
        else MagicMock(return_value=balance_result or {"total": {"USDT": 100}})
    )
    client.fetch_account_info = (
        MagicMock(side_effect=account_info_error)
        if account_info_error is not None
        else MagicMock(return_value=account_info_result or account_info_response())
    )
    client.fetch_deposits = MagicMock(return_value=deposits or [])
    client.fetch_withdrawals = MagicMock(return_value=withdrawals or [])
    client.fetch_my_trades = MagicMock(return_value=trades or [])
    return client


def test_read_only_key_is_accepted():
    client = _client_with(account_info_result=account_info_response(authorities="spot_account_r,spot_order_r"))
    ok, reason = verify_read_only(client)
    assert ok, reason


def test_key_with_trade_write_scope_is_rejected():
    client = _client_with(account_info_result=account_info_response(authorities="spot_account_r,spot_trade_w"))
    ok, reason = verify_read_only(client)
    assert not ok


def test_key_with_withdraw_keyword_is_rejected():
    client = _client_with(account_info_result=account_info_response(authorities="withdraw_w"))
    ok, reason = verify_read_only(client)
    assert not ok


def test_missing_authorities_field_fails_closed():
    resp = account_info_response()
    resp["data"].pop("authorities")
    client = _client_with(account_info_result=resp)
    ok, reason = verify_read_only(client)
    assert not ok
    assert "authorities" in reason


def test_balance_call_failure_rejects():
    client = _client_with(balance_error=TamperSignal("invalid key"))
    ok, reason = verify_read_only(client)
    assert not ok


def test_onboard_account_persists_encrypted_on_success(db, cipher, monkeypatch):
    def fake_client_ctor(exchange, api_key, secret, passphrase=""):
        return _client_with(account_info_result=account_info_response(authorities="spot_account_r"))

    monkeypatch.setattr("guardian.onboarding.make_client", fake_client_ctor)

    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-1", topology="independent", api_key="k", secret="s", passphrase="p"
    )
    assert result.label == "acct-1"

    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (result.account_id,))
        row = cur.fetchone()
    assert row["encrypted_api_key"] != "k"
    assert cipher.decrypt(row["encrypted_api_key"]) == "k"

    # "On success" has to mean the whole onboarding succeeded, not just the
    # part this test looks at. Two of the baseline's exchange reads failed on
    # every run of this test - they were reaching the real Bitget and being
    # rejected - and `seed_baseline` swallows that by design, recording a gap
    # and carrying on. The assertion above passed throughout. Reading the
    # account's own record is what tells them apart.
    with db.cursor() as cur:
        cur.execute(
            "SELECT payload_json FROM guardian_events "
            "WHERE account_id = %s AND event_type = 'baseline_seed_incomplete'",
            (result.account_id,),
        )
        gaps = [row["payload_json"] for row in cur.fetchall()]
    assert gaps == [], f"the baseline did not seed completely: {gaps}"


def test_onboard_account_rejects_write_scope_and_persists_nothing(db, cipher, monkeypatch):
    def fake_client_ctor(exchange, api_key, secret, passphrase=""):
        return _client_with(account_info_result=account_info_response(authorities="spot_trade_w"))

    monkeypatch.setattr("guardian.onboarding.make_client", fake_client_ctor)

    with pytest.raises(OnboardingRejected):
        onboard_account(db, cipher, user_id=TEST_USER_ID, label="acct-2", topology="independent", api_key="k", secret="s", passphrase="p"
        )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts")
        rows = cur.fetchall()
    assert len(rows) == 0
