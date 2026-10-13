"""A customer replacing their own revoked monitoring key (#69).

The runbook tells a frightened person to revoke API keys and names ours as the
one to leave alone. Some will revoke everything anyway - that is why step 2 is
worded the way it is - and until now the only way back was an operator being
awake. This is the self-serve route, and it is the portal's first write that
accepts a secret.

The tests are ordered by what they cost if wrong:

- a valid, read-only key **for a different exchange account** is refused. This
  is the whole reason the feature is not three lines: such a key passes G-4,
  stores cleanly, and leaves us polling a quiet account while the customer's
  real one is drained and the portal says "watching";
- an account with no recorded exchange identity is refused rather than
  guessed at, and somebody is told the customer is stuck;
- a key that can trade or withdraw is refused, exactly as at onboarding;
- nothing is stored on any refusal;
- no part of a submitted key reaches an event payload, a page, or a log.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from guardian.baseline import learn_exchange_uid
from guardian.exchange import exchange_account_uid
from guardian.keyreplacement import replace_monitoring_key
from tests.guardian.fixtures.ccxt_responses import account_info_response
from tests.guardian.conftest import TEST_USER_ID
from tests.guardian.conftest import make_user
from tests.guardian.conftest import SHIPPED_RULES

OUR_UID = "12345"
SOMEBODY_ELSES_UID = "99999"


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


def _account(db, cipher, *, uid=OUR_UID, label="acct-1", email="alice@example.com"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_accounts (user_id, onboarding_source, label, exchange, topology, encrypted_api_key, encrypted_secret, "
            "encrypted_passphrase, exchange_uid, status, onboarded_at, updated_at) "
            "VALUES (%s, 'self', %s, 'bitget', 'independent', %s, %s, %s, %s, 'active', '2026-01-01', '2026-01-01')",
            (make_user(db, email), label, cipher.encrypt("old-key"), cipher.encrypt("old-secret"),
             cipher.encrypt("old-pass"), uid),
        )
        account_id = cur.lastrowid
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))
        return account_id


def _stored(db, account_id, cipher):
    with db.cursor() as cur:
        cur.execute("SELECT encrypted_api_key, encrypted_secret, encrypted_passphrase, updated_at "
                    "FROM guardian_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    return {
        "api_key": cipher.decrypt(row["encrypted_api_key"]),
        "secret": cipher.decrypt(row["encrypted_secret"]),
        "passphrase": cipher.decrypt(row["encrypted_passphrase"]),
        "updated_at": row["updated_at"],
    }


def _events(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT event_type, payload_json, actor FROM guardian_events WHERE account_id = %s",
                    (account_id,))
        return [dict(r) for r in cur.fetchall()]


@pytest.fixture
def client_for(monkeypatch):
    """Replaces `make_client` in the module under test, and hands back a
    recorder so a test can say what the exchange answers.

    The client is a mock rather than a real `BitgetClient` with mocked methods:
    #68 is a record of what happens when a test builds a real client and misses
    one of the seven calls something further down makes."""
    made = []

    def _install(*, authorities="spot_account_r", uid=OUR_UID, balance_raises=None,
                 info_raises=None, build_raises=None):
        def _make_client(exchange, api_key, secret, passphrase):
            if build_raises is not None:
                raise build_raises
            made.append({"exchange": exchange, "api_key": api_key,
                         "secret": secret, "passphrase": passphrase})
            client = MagicMock()
            client.exchange_id = exchange
            if balance_raises is not None:
                client.fetch_balance.side_effect = balance_raises
            else:
                client.fetch_balance.return_value = {"total": {"USDT": 100}}
            info = account_info_response(authorities=authorities)
            if uid is None:
                info["data"].pop("userId")
            else:
                info["data"]["userId"] = uid
            if info_raises is not None:
                client.fetch_account_info.side_effect = info_raises
            else:
                client.fetch_account_info.return_value = info
            return client

        monkeypatch.setattr("guardian.keyreplacement.make_client", _make_client)
        return made

    return _install


# --- the attack the design exists for ----------------------------------------


def test_a_read_only_key_for_a_different_account_is_refused(db, cipher, client_for):
    """The centrepiece. This key is real, it is read-only, and it works - it
    is simply for somebody else's exchange account.

    Store it and every downstream signal turns green: polls succeed, no
    detector fires, the portal reports the account as watched. The customer's
    real account is unmonitored and nothing anywhere says so. It is the exact
    failure this product exists to prevent, and it would have been introduced
    by the feature meant to help."""
    client_for(uid=SOMEBODY_ELSES_UID)
    account_id = _account(db, cipher)
    alerter = _Alerter()

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="new-key", secret="new-secret",
        passphrase="new-pass", actor="alice@example.com", operator_alerter=alerter)

    assert not outcome.ok
    assert outcome.reason_code == "wrong_account"
    assert _stored(db, account_id, cipher)["api_key"] == "old-key"
    assert "different exchange account" in outcome.message
    # And somebody is told, because the innocent reading of this - a customer
    # pasting the wrong account's key mid-incident - ends with them believing
    # they are protected.
    assert alerter.rules() == ["monitoring_key_replacement_wrong_account"]
    assert alerter.pages[0]["severity"] == "HIGH"


def test_the_refusal_does_not_tell_the_customer_the_other_account_id(db, cipher, client_for):
    """Both ids go to the operator page; neither goes on the screen. The
    person reading the screen may not be the account's owner."""
    client_for(uid=SOMEBODY_ELSES_UID)
    account_id = _account(db, cipher)
    alerter = _Alerter()

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="k", secret="s",
        actor="alice@example.com", operator_alerter=alerter)

    assert OUR_UID not in outcome.message
    assert SOMEBODY_ELSES_UID not in outcome.message
    evidence = alerter.pages[0]["evidence"]
    assert evidence["expected_account"] == OUR_UID
    assert evidence["submitted_account"] == SOMEBODY_ELSES_UID


# --- fail closed where we cannot check ----------------------------------------


def test_an_account_with_no_recorded_identity_is_refused_and_paged(db, cipher, client_for):
    """Every account onboarded before `exchange_uid` existed starts NULL, and
    fills in on its next slow loop. In that window there is nothing to check a
    replacement against - so the answer is the same as everywhere else in this
    codebase when it cannot tell: refuse.

    And page. A customer who reaches this is stuck at the worst possible
    moment, and an unread refusal looks exactly like a service that works."""
    client_for()
    account_id = _account(db, cipher, uid=None)
    alerter = _Alerter()

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="new-key", secret="new-secret",
        actor="alice@example.com", operator_alerter=alerter)

    assert not outcome.ok
    assert outcome.reason_code == "no_recorded_identity"
    assert _stored(db, account_id, cipher)["api_key"] == "old-key"
    assert alerter.rules() == ["monitoring_key_replacement_needs_an_operator"]
    # The customer is told a person now knows, because "contact us" with no
    # evidence anyone was contacted is how people wait for nothing.
    assert "Minas has been told" in outcome.message


def test_a_payload_with_no_account_id_is_refused(db, cipher, client_for):
    """Binance's apiRestrictions carries no account identifier at all, so this
    is Binance's permanent state rather than an error - and it must refuse
    rather than fall through to "well, the key is read-only"."""
    client_for(uid=None)
    account_id = _account(db, cipher)

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="k", secret="s", actor="alice@example.com")

    assert not outcome.ok
    assert outcome.reason_code == "identity_unreadable"
    assert _stored(db, account_id, cipher)["api_key"] == "old-key"


def test_an_unreadable_account_info_is_refused(db, cipher, client_for):
    client_for(info_raises=RuntimeError("exchange having a bad day"))
    account_id = _account(db, cipher)

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="k", secret="s", actor="alice@example.com")

    assert not outcome.ok
    assert _stored(db, account_id, cipher)["api_key"] == "old-key"


# --- the read-only guarantee, on a second door --------------------------------


@pytest.mark.parametrize("authorities", ["spot_account_r,spot_trade_w", "withdraw_w"])
def test_a_key_that_can_trade_or_withdraw_is_refused(db, cipher, client_for, authorities):
    """G-4 applies to every door a key can arrive through, not just the one an
    operator uses. Accepting one here with a warning would make the read-only
    guarantee a preference."""
    client_for(authorities=authorities)
    account_id = _account(db, cipher)

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="k", secret="s", actor="alice@example.com")

    assert not outcome.ok
    assert outcome.reason_code == "not_read_only"
    assert _stored(db, account_id, cipher)["api_key"] == "old-key"
    assert "read-only" in outcome.message


def test_a_key_that_cannot_read_the_balance_is_refused(db, cipher, client_for):
    client_for(balance_raises=RuntimeError("401"))
    account_id = _account(db, cipher)

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="k", secret="s", actor="alice@example.com")

    assert not outcome.ok
    assert _stored(db, account_id, cipher)["api_key"] == "old-key"


@pytest.mark.parametrize("api_key,secret", [("", "s"), ("k", ""), ("   ", "s")])
def test_an_incomplete_submission_never_reaches_the_exchange(db, cipher, client_for, api_key, secret):
    made = client_for()
    account_id = _account(db, cipher)

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key=api_key, secret=secret, actor="alice@example.com")

    assert not outcome.ok
    assert outcome.reason_code == "incomplete"
    assert made == [], "a half-filled form made us call the exchange"


def test_an_unknown_account_is_refused(db, cipher, client_for):
    client_for()
    outcome = replace_monitoring_key(
        db, cipher, account_id=4242, api_key="k", secret="s", actor="alice@example.com")
    assert not outcome.ok
    assert outcome.reason_code == "no_such_account"


# --- the happy path -----------------------------------------------------------


def test_a_matching_read_only_key_replaces_the_stored_one(db, cipher, client_for):
    made = client_for()
    account_id = _account(db, cipher)
    before = _stored(db, account_id, cipher)
    alerter = _Alerter()

    outcome = replace_monitoring_key(
        db, cipher, account_id=account_id, api_key="new-key", secret="new-secret",
        passphrase="new-pass", actor="alice@example.com", operator_alerter=alerter)

    assert outcome.ok, outcome.detail
    after = _stored(db, account_id, cipher)
    assert after["api_key"] == "new-key"
    assert after["secret"] == "new-secret"
    assert after["passphrase"] == "new-pass"
    assert after["updated_at"] != before["updated_at"]
    # The key was validated with the credentials submitted, not the stored ones.
    assert made[0]["api_key"] == "new-key"


def test_the_replacement_is_recorded_against_the_member_who_made_it(db, cipher, client_for):
    """A credential change with nobody's name on it is the shape of an
    unauthorised one (G-32, G-50). It has to appear on the page a customer
    reads when asking who has touched their account."""
    from guardian.events import ADMIN_EVENT_LABELS, ADMIN_EVENT_TYPES

    client_for()
    account_id = _account(db, cipher)

    replace_monitoring_key(db, cipher, account_id=account_id, api_key="new-key",
                           secret="new-secret", actor="alice@example.com")

    events = [e for e in _events(db, account_id) if e["event_type"] == "monitoring_key_replaced"]
    assert len(events) == 1
    assert events[0]["actor"] == "alice@example.com"
    assert "monitoring_key_replaced" in ADMIN_EVENT_TYPES
    assert ADMIN_EVENT_LABELS["monitoring_key_replaced"]


def test_an_operator_is_told_about_a_successful_replacement(db, cipher, client_for):
    """A key replacement during an incident is a business event, and an
    unexpected one is itself a signal."""
    client_for()
    account_id = _account(db, cipher)
    alerter = _Alerter()

    replace_monitoring_key(db, cipher, account_id=account_id, api_key="k", secret="s",
                           actor="alice@example.com", operator_alerter=alerter)

    assert alerter.rules() == ["monitoring_key_replaced"]


def test_a_dead_pager_does_not_undo_an_accepted_key(db, cipher, client_for):
    """The decision is made and recorded before anyone is paged. A mail server
    that is down must not turn an installed key into an error the customer
    retries - nor a refusal into an acceptance."""
    client_for()
    account_id = _account(db, cipher)

    outcome = replace_monitoring_key(db, cipher, account_id=account_id, api_key="new-key",
                                     secret="s", actor="alice@example.com",
                                     operator_alerter=_Alerter(explode=True))

    assert outcome.ok
    assert _stored(db, account_id, cipher)["api_key"] == "new-key"


# --- secrets do not leak ------------------------------------------------------


def test_no_part_of_the_key_reaches_the_record_or_the_screen(db, cipher, client_for, caplog):
    """The event row is readable in the portal and kept for two years; the
    operator page goes out by email. Neither carries a fragment of the key, not
    even a prefix (G-41)."""
    import logging

    caplog.set_level(logging.DEBUG)
    client_for()
    account_id = _account(db, cipher)
    alerter = _Alerter()
    key, secret = "SUPER-SECRET-KEY", "SUPER-SECRET-SECRET"

    outcome = replace_monitoring_key(db, cipher, account_id=account_id, api_key=key,
                                     secret=secret, actor="alice@example.com",
                                     operator_alerter=alerter)

    haystack = "\n".join([
        outcome.message, outcome.detail,
        json.dumps(_events(db, account_id)),
        json.dumps(alerter.pages, default=str),
        caplog.text,
    ])
    for fragment in (key, secret, key[:6], secret[:6]):
        assert fragment not in haystack


def test_a_refused_key_is_not_written_anywhere_either(db, cipher, client_for, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    client_for(uid=SOMEBODY_ELSES_UID)
    account_id = _account(db, cipher)
    alerter = _Alerter()

    outcome = replace_monitoring_key(db, cipher, account_id=account_id, api_key="REFUSED-KEY",
                                     secret="REFUSED-SECRET", actor="alice@example.com",
                                     operator_alerter=alerter)

    haystack = "\n".join([outcome.message, outcome.detail,
                          json.dumps(alerter.pages, default=str), caplog.text])
    assert "REFUSED-KEY" not in haystack
    assert "REFUSED-SECRET" not in haystack


# --- the identity these checks rest on ----------------------------------------


@pytest.mark.parametrize("exchange,payload,expected", [
    ("bitget", {"data": {"userId": "12345"}}, "12345"),
    ("bitget", {"userId": "12345"}, "12345"),
    ("bitget", {"data": {"userId": 12345}}, "12345"),
    ("bitget", {"data": {"userId": "  12345  "}}, "12345"),
    # Zero is an id, not an absence. The falsy-sentinel rule, on a new field.
    ("bitget", {"data": {"userId": 0}}, "0"),
    ("bitget", {"data": {"userId": ""}}, None),
    ("bitget", {"data": {"userId": None}}, None),
    ("bitget", {"data": {}}, None),
    ("bybit", {"data": {"userID": "77"}}, "77"),
    ("okx", {"data": {"uid": "88"}}, "88"),
    # Binance's payload has no account id; the empty tuple is deliberate.
    ("binance", {"data": {"userId": "12345", "uid": "12345"}}, None),
    ("kraken", {"data": {"uid": "1"}}, None),
    ("bitget", None, None),
    ("bitget", "not a dict", None),
])
def test_exchange_account_uid(exchange, payload, expected):
    assert exchange_account_uid(exchange, payload) == expected


def test_a_recorded_identity_is_never_overwritten(db, cipher):
    """It is the thing replacements are checked against. A path that updated it
    would let the check be walked past one poll at a time."""
    account_id = _account(db, cipher, uid=OUR_UID)

    assert learn_exchange_uid(db, account_id, OUR_UID) == "unchanged"
    assert learn_exchange_uid(db, account_id, SOMEBODY_ELSES_UID) == "mismatch"

    with db.cursor() as cur:
        cur.execute("SELECT exchange_uid FROM guardian_accounts WHERE id = %s", (account_id,))
        assert cur.fetchone()["exchange_uid"] == OUR_UID


def test_an_empty_identity_is_learned_once(db, cipher):
    account_id = _account(db, cipher, uid=None)

    assert learn_exchange_uid(db, account_id, None) == "unknown"
    assert learn_exchange_uid(db, account_id, "   ") == "unknown"
    assert learn_exchange_uid(db, account_id, OUR_UID) == "learned"
    assert learn_exchange_uid(db, account_id, OUR_UID) == "unchanged"


def test_learning_about_an_account_that_is_gone_says_unknown(db, cipher):
    assert learn_exchange_uid(db, 4242, OUR_UID) == "unknown"


# --- and something has to record it in the first place ------------------------
#
# The three tests below are the ones that make the rest of this file mean
# anything. A check against a column nothing populates refuses every honest
# customer forever, which is a worse product than no feature at all - and it
# would look exactly like working code.


def _exchange_client(authorities="spot_account_r", uid=OUR_UID):
    client = MagicMock()
    client.exchange_id = "bitget"
    client.fetch_balance.return_value = {"total": {"BTC": 1.0}}
    client.fetch_deposits.return_value = []
    client.fetch_withdrawals.return_value = []
    client.fetch_closed_orders.return_value = []
    client.fetch_transfers.return_value = []
    client.fetch_my_trades.return_value = []
    client.fetch_tickers.return_value = {}
    info = account_info_response(authorities=authorities)
    if uid is None:
        info["data"].pop("userId")
    else:
        info["data"]["userId"] = uid
    client.fetch_account_info.return_value = info
    return client


def _recorded_uid(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT exchange_uid FROM guardian_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()["exchange_uid"]


def test_onboarding_records_the_exchange_account_id(db, cipher, monkeypatch):
    """Onboarding is the one moment the key and the account are known to belong
    together, because a person put them there."""
    from guardian.onboarding import onboard_account

    monkeypatch.setattr("guardian.onboarding.make_client", lambda *a, **k: _exchange_client())
    result = onboard_account(db, cipher, user_id=TEST_USER_ID, label="fresh", topology="independent",
                             api_key="k", secret="s", passphrase="p")

    assert _recorded_uid(db, result.account_id) == OUR_UID


def _slow_loop(db, cipher, account_id, client, monkeypatch):
    from guardian.config import RulesConfig
    from guardian.poller import AccountPoller

    monkeypatch.setattr("guardian.poller.make_client", lambda *a, **k: client)
    poller = AccountPoller(db, cipher, RulesConfig(SHIPPED_RULES),
                           {"CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": []})
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    poller.run_slow_loop(row)


def test_the_slow_loop_learns_the_id_for_an_account_that_predates_the_column(
        db, cipher, monkeypatch):
    """Every account already in the fleet starts NULL. Without this they would
    stay NULL forever and self-serve replacement would be a page that always
    says no - so the ten-minute poll fills it in, and the whole fleet becomes
    eligible within one slow loop rather than never."""
    account_id = _account(db, cipher, uid=None)

    _slow_loop(db, cipher, account_id, _exchange_client(), monkeypatch)

    assert _recorded_uid(db, account_id) == OUR_UID


def test_the_slow_loop_does_not_overwrite_an_id_it_disagrees_with(db, cipher, monkeypatch, caplog):
    """A recorded id that can be moved by a poll is not a check, it is a delay.

    Not made into a detection either: the same stored key reporting a different
    account is far more likely to be an exchange changing its JSON than an
    attack, and a CRITICAL that fires on a vendor's release note is how alert
    fatigue starts. Logged, and left alone."""
    import logging

    caplog.set_level(logging.WARNING)
    account_id = _account(db, cipher, uid=OUR_UID)

    _slow_loop(db, cipher, account_id, _exchange_client(uid=SOMEBODY_ELSES_UID), monkeypatch)

    assert _recorded_uid(db, account_id) == OUR_UID
    assert "different account id" in caplog.text
