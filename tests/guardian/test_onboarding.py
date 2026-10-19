from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from guardian.exchange import BitgetClient, TamperSignal
from guardian.onboarding import (
    NOT_READ_ONLY_MESSAGE,
    AlreadyMonitored,
    OnboardingRejected,
    generate_unique_label,
    onboard_account,
    seed_account_baseline,
    verify_read_only,
)
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID, make_account, make_user


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


def test_a_long_customer_name_still_yields_a_label_that_fits(db):
    """`label` is varchar(100). An unbounded slug failed the INSERT with a
    truncation error - after the exchange key had been verified, so the
    customer had done everything right and the error named a database type."""
    name = ("Customer " * 25)[:200]
    assert len(name) == 200

    label = generate_unique_label(db, name)
    assert len(label) <= 100
    assert label.startswith("customer-customer")
    assert not label.endswith("-")

    make_account(db, label)
    second = generate_unique_label(db, name)
    assert second != label
    assert second.startswith(label)
    assert len(second) <= 100


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


# --- the Bitget gate on the platform's confirmed permission table (v0.9.2) ----
#
# Bitget's real codes are four letters; the fixtures above use synthetic
# `..._r` / `..._w` names that the documented suffix convention still covers.
# These tests use the codes seen on live keys (api/services/bitget_permissions.py).


def _unified_info(perm_type="read-only", permissions=("uta_mgt",), uid="12345"):
    """/api/v3/account/info as a Unified Trading Account answers it."""
    return {"code": "00000", "msg": "success",
            "data": {"userId": uid, "permType": perm_type, "permissions": list(permissions), "ips": ""}}


def test_the_read_scopes_taxw_and_p2pw_are_accepted():
    """Taxation -> Search and P2P -> Search end in 'w' and are reads. The old
    gate refused them, which turned away honest read-only keys."""
    client = _client_with(account_info_result=account_info_response(authorities="stor,taxw,p2pw"))
    ok, reason = verify_read_only(client)
    assert ok, reason


@pytest.mark.parametrize("code,kind", [
    ("stow", "spot trade"),         # Spot -> Trade
    ("wtow", "wallet transfer"),    # Wallet -> Transfer between the customer's own accounts
    ("smow", "margin trade"),       # Margin -> Trade
    ("withdraw", "withdraw"),
    ("transfer", "transfer"),
    ("trade", "trade"),
])
def test_every_trade_withdraw_and_transfer_code_is_refused_by_name(code, kind):
    client = _client_with(account_info_result=account_info_response(authorities=f"stor,{code}"))
    ok, reason = verify_read_only(client)
    assert not ok
    assert code in reason and kind in reason


@pytest.mark.parametrize("authorities", ["", "   ", ",", "zzzz", "stor,zzzz"])
def test_an_empty_list_and_an_unknown_code_fail_closed(authorities):
    client = _client_with(account_info_result=account_info_response(authorities=authorities))
    ok, reason = verify_read_only(client)
    assert not ok
    if "zzzz" in authorities:
        assert "zzzz" in reason, "the refusal names the code nobody has confirmed"
    else:
        assert "empty" in reason


def test_a_unified_read_only_key_is_accepted():
    client = _client_with(account_info_result=_unified_info("read-only", ["uta_mgt"]))
    ok, reason = verify_read_only(client)
    assert ok, reason


@pytest.mark.parametrize("perm_type,permissions,named", [
    ("read-and-write", ["uta_trade", "uta_mgt"], "read-and-write"),
    ("read-only", ["uta_trade", "uta_mgt"], "uta_trade"),      # the flag and the tokens must agree
    ("read-only", ["uta_mgt", "withdraw"], "withdraw"),
    ("", ["uta_mgt"], "permType"),
])
def test_a_unified_key_that_can_trade_or_withdraw_is_refused(perm_type, permissions, named):
    client = _client_with(account_info_result=_unified_info(perm_type, permissions))
    ok, reason = verify_read_only(client)
    assert not ok
    assert named in reason


def test_a_refusal_reads_as_plain_language_and_keeps_the_detail_for_the_log(db, cipher, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor,stow")))

    with pytest.raises(OnboardingRejected) as info:
        onboard_account(db, cipher, user_id=TEST_USER_ID, label="loud", topology="independent",
                        api_key="k", secret="s", passphrase="p")

    assert str(info.value) == NOT_READ_ONLY_MESSAGE
    assert "stow" not in str(info.value) and "token" not in str(info.value) and "authorit" not in str(info.value)
    assert "stow" in info.value.reason
    assert "stow" in caplog.text


# --- account_mode ---------------------------------------------------------------------


def _account_row(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()


def _events(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT event_type, severity, payload_json, actor FROM guardian_events WHERE account_id = %s ORDER BY id",
                    (account_id,))
        return [dict(r) for r in cur.fetchall()]


def _count(db, table, account_id):
    with db.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {table} WHERE account_id = %s", (account_id,))
        return cur.fetchone()["n"]


def _onboard(db, cipher, label="acct", api_key="k", exchange="bitget", **kwargs):
    return onboard_account(db, cipher, user_id=TEST_USER_ID, label=label, topology="independent",
                           api_key=api_key, secret="s", passphrase="p", exchange=exchange, **kwargs)


def test_account_mode_is_recorded_from_the_permission_payload(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor")))
    classic = _onboard(db, cipher, label="classic", api_key="classic-key")
    assert _account_row(db, classic.account_id)["account_mode"] == "classic"

    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=_unified_info(uid="777")))
    unified = _onboard(db, cipher, label="unified", api_key="unified-key")
    assert _account_row(db, unified.account_id)["account_mode"] == "unified"


def test_account_mode_stays_null_when_the_exchange_does_not_say(db, cipher, monkeypatch):
    client = MagicMock()
    client.exchange_id = "okx"
    client.fetch_balance.return_value = {"total": {"BTC": 1.0}}
    client.fetch_account_info.return_value = {"data": {"perm": "read_only", "uid": "88", "authorities": "read_only"}}
    for name in ("fetch_deposits", "fetch_withdrawals", "fetch_closed_orders", "fetch_transfers", "fetch_my_trades"):
        getattr(client, name).return_value = []
    client.fetch_tickers.return_value = {}
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client)

    result = _onboard(db, cipher, label="okx-acct", exchange="okx")

    row = _account_row(db, result.account_id)
    assert row["account_mode"] is None
    assert row["exchange_uid"] == "88"


def test_customer_name_is_bounded_to_the_column(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor")))
    result = _onboard(db, cipher, label="long-name", customer_name="N" * 300)
    assert len(_account_row(db, result.account_id)["customer_name"]) == 255


# --- seed=False and seed_account_baseline ----------------------------------------------


def _engine_would_poll(db):
    """The predicate the engine uses to pick accounts (guardian/engine.py)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM guardian_accounts WHERE status = 'active' AND baseline_status <> 'seeding' ORDER BY id")
        return [r["id"] for r in cur.fetchall()]


class _Alerter:
    """Records pages instead of sending them."""

    def __init__(self, explode=False):
        self.pages = []
        self._explode = explode

    def page(self, **kwargs):
        if self._explode:
            raise RuntimeError("smtp is down")
        self.pages.append(kwargs)
        return 1

    def rules(self):
        return [p["rule"] for p in self.pages]


def test_seed_false_leaves_the_row_seeding_and_schedules_nothing(db, cipher, monkeypatch):
    client = _client_with(account_info_result=account_info_response(authorities="stor"))
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client)

    result = _onboard(db, cipher, label="later", seed=False)

    row = _account_row(db, result.account_id)
    assert row["baseline_status"] == "seeding"
    assert row["status"] == "active"
    assert row["exchange_uid"] == "12345", "the uid is learned from the payload the gate verified"
    # The gate ran (two reads); the baseline did not.
    client.fetch_balance.assert_called_once()
    client.fetch_account_info.assert_called_once()
    client.fetch_deposits.assert_not_called()
    client.fetch_withdrawals.assert_not_called()
    client.fetch_closed_orders.assert_not_called()
    client.fetch_transfers.assert_not_called()
    assert _count(db, "guardian_balance_snapshots", result.account_id) == 0
    assert _count(db, "guardian_permission_snapshots", result.account_id) == 0
    assert _count(db, "guardian_poll_state", result.account_id) == 1
    # The engine does not pick it up while it says 'seeding'.
    assert _engine_would_poll(db) == []
    # Adding the account was still recorded.
    assert [e["event_type"] for e in _events(db, result.account_id)] == ["customer_added"]


def test_seed_account_baseline_rebuilds_the_client_from_the_stored_credentials_and_marks_complete(db, cipher, monkeypatch):
    made = []

    def ctor(exchange, api_key, secret, passphrase=""):
        made.append((exchange, api_key, secret, passphrase))
        return _client_with(account_info_result=account_info_response(authorities="stor"))

    monkeypatch.setattr("guardian.onboarding.make_client", ctor)
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="bg", topology="independent",
                             api_key="SEED-KEY", secret="SEED-SECRET", passphrase="SEED-PASS", seed=False)
    alerter = _Alerter()

    status = seed_account_baseline(db, cipher, result.account_id, actor="user:test", operator_alerter=alerter)

    assert status == "complete"
    assert _account_row(db, result.account_id)["baseline_status"] == "complete"
    assert made[-1] == ("bitget", "SEED-KEY", "SEED-SECRET", "SEED-PASS"), "decrypted from the row, not re-submitted"
    assert _count(db, "guardian_balance_snapshots", result.account_id) >= 1
    assert _count(db, "guardian_permission_snapshots", result.account_id) == 1
    assert _engine_would_poll(db) == [result.account_id]
    assert alerter.pages == []
    assert [e["event_type"] for e in _events(db, result.account_id)] == ["customer_added"]


def test_seed_account_baseline_marks_incomplete_when_part_of_the_history_cannot_be_read(db, cipher, monkeypatch):
    client = _client_with(account_info_result=account_info_response(authorities="stor"))
    client.fetch_closed_orders = MagicMock(side_effect=RuntimeError("order history endpoint down"))
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client)
    result = _onboard(db, cipher, label="partial", seed=False)
    alerter = _Alerter()

    status = seed_account_baseline(db, cipher, result.account_id, actor="user:test", operator_alerter=alerter)

    assert status == "incomplete"
    assert _account_row(db, result.account_id)["baseline_status"] == "incomplete"
    events = _events(db, result.account_id)
    gaps = [e for e in events if e["event_type"] == "baseline_seed_incomplete"]
    assert len(gaps) == 1 and json.loads(gaps[0]["payload_json"])["what"] == "closed_orders"
    assert gaps[0]["actor"] == "user:test"
    assert alerter.rules() == ["account_onboarded_with_partial_baseline"]
    assert alerter.pages[0]["account_id"] == result.account_id
    assert _engine_would_poll(db) == [result.account_id], "incomplete is polled; only seeding is not"


@pytest.mark.parametrize("failure", ["exchange_refuses", "client_cannot_be_built", "exchange_error_names_the_key"])
def test_seed_account_baseline_never_leaves_a_row_seeding(db, cipher, monkeypatch, caplog, failure):
    """The second half of a background onboarding. Whatever the exchange or
    the process does, the row ends 'incomplete' with a 'baseline_seed_failed'
    event, so the engine polls it and an operator knows to re-seed it. A row
    left at 'seeding' is an account nobody watches and nobody is told about."""
    caplog.set_level(logging.DEBUG)
    key, secret = "SEED-KEY-0123456789", "SEED-SECRET-0123456789"
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor")))
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="unlucky", topology="independent",
                             api_key=key, secret=secret, passphrase="p", seed=False)

    if failure == "exchange_refuses":
        broken = _client_with(balance_error=TamperSignal("api key revoked"))
        monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: broken)
    elif failure == "client_cannot_be_built":
        def explode(*a, **k):
            raise RuntimeError("ccxt could not build the client")
        monkeypatch.setattr("guardian.onboarding.make_client", explode)
    else:
        broken = _client_with(balance_error=RuntimeError(f"401 unauthorized for key {key} secret {secret}"))
        monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: broken)
    alerter = _Alerter()

    status = seed_account_baseline(db, cipher, result.account_id, actor="user:test", operator_alerter=alerter)

    assert status == "incomplete"
    row = _account_row(db, result.account_id)
    assert row["baseline_status"] == "incomplete"
    events = _events(db, result.account_id)
    failed = [e for e in events if e["event_type"] == "baseline_seed_failed"]
    assert len(failed) == 1
    payload = json.loads(failed[0]["payload_json"])
    assert payload["error"] and "consequence" in payload
    assert failed[0]["severity"] == "HIGH" and failed[0]["actor"] == "user:test"
    assert alerter.rules() == ["account_baseline_seed_failed"]
    assert _engine_would_poll(db) == [result.account_id]
    # No secret reaches the record, the page or the log - even when the
    # exchange's own error message carried it.
    haystack = "\n".join([json.dumps(events), json.dumps(alerter.pages, default=str), caplog.text])
    for fragment in (key, secret, key[:12], secret[:14]):
        assert fragment not in haystack, fragment


def test_seed_account_baseline_survives_a_dead_pager_and_an_unknown_account(db, cipher, monkeypatch):
    client = _client_with(account_info_result=account_info_response(authorities="stor"))
    client.fetch_closed_orders = MagicMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client)
    result = _onboard(db, cipher, label="pager", seed=False)

    assert seed_account_baseline(db, cipher, result.account_id, operator_alerter=_Alerter(explode=True)) == "incomplete"
    assert _account_row(db, result.account_id)["baseline_status"] == "incomplete"
    # An id that names no row: nothing to mark, nothing raised.
    assert seed_account_baseline(db, cipher, 424242) == "incomplete"


# --- one exchange account, one monitoring key per owner -----------------------------------


def test_a_second_key_for_the_same_exchange_account_is_refused_as_already_monitored(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor")))
    first = _onboard(db, cipher, label="main", api_key="key-one")

    with pytest.raises(AlreadyMonitored) as info:
        _onboard(db, cipher, label="main-2", api_key="key-two")

    assert isinstance(info.value, OnboardingRejected)
    assert info.value.label == "main"
    assert str(info.value) == ("This exchange account is already being watched as main. Add a different account, "
                               "or replace the key on the existing one.")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM guardian_accounts")
        assert [r["id"] for r in cur.fetchall()] == [first.account_id], "nothing was inserted"


def test_a_disabled_row_does_not_block_watching_the_account_again(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor")))
    first = _onboard(db, cipher, label="main", api_key="key-one")
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET status = 'disabled' WHERE id = %s", (first.account_id,))

    second = _onboard(db, cipher, label="main-2", api_key="key-two")

    assert second.account_id != first.account_id
    assert _account_row(db, second.account_id)["exchange_uid"] == "12345"


def test_the_same_exchange_account_under_another_owner_is_not_this_owners_duplicate(db, cipher, monkeypatch):
    monkeypatch.setattr("guardian.onboarding.make_client",
                        lambda *a, **k: _client_with(account_info_result=account_info_response(authorities="stor")))
    _onboard(db, cipher, label="mine", api_key="key-one")
    other = make_user(db, "other-owner@example.com")

    result = onboard_account(db, cipher, user_id=other, label="theirs", topology="independent",
                             api_key="key-two", secret="s", passphrase="p")

    assert _account_row(db, result.account_id)["user_id"] == other


def test_a_payload_without_an_account_id_cannot_be_checked_for_duplicates(db, cipher, monkeypatch):
    """Binance's apiRestrictions carries no uid; two keys for one Binance
    account cannot be told apart here, and the fingerprint still refuses the
    same KEY twice."""
    resp = account_info_response(authorities="stor")
    resp["data"].pop("userId")
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _client_with(account_info_result=resp))
    _onboard(db, cipher, label="no-uid", api_key="key-one")
    second = _onboard(db, cipher, label="no-uid-2", api_key="key-two")
    assert _account_row(db, second.account_id)["exchange_uid"] is None


def test_a_unified_bitget_key_is_refused_with_a_message_that_says_so(db, cipher, monkeypatch):
    """Guardian's Bitget client reads the Classic (v2) endpoints; a Unified Trading
    account's key is answered with 40085 and must be refused with a message naming the
    cause, not the generic 'not read-only' one. Nothing is stored."""
    from guardian.onboarding import UNIFIED_REASON_PREFIX, onboard_account, OnboardingRejected
    err = Exception('bitget {"code":"40085","msg":"You are in Unified Account mode, and the Classic Account API is not supported"}')
    client = _client_with(balance_error=err)
    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: client)
    with pytest.raises(OnboardingRejected) as e:
        onboard_account(db, cipher, label="uta-key", topology="independent", api_key="uta-k", secret="s",
                        passphrase="p", exchange="bitget", user_id=TEST_USER_ID, source="self")
    assert "Unified Trading" in str(e.value) and "cannot watch yet" in str(e.value)
    assert e.value.reason.startswith(UNIFIED_REASON_PREFIX)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM guardian_accounts WHERE label = %s", ("uta-key",))
        assert cur.fetchone()["n"] == 0
