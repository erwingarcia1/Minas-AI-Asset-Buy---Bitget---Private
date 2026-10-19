"""`guardian.accounts`: a customer's accounts as the API manages them (PR3).

Ordered by what it costs if wrong: a row shown to the wrong customer, a key
left behind by a deletion, a contact value written into the event log, and
a removed account counted against (or sneaking past) the ten-account cap.
"""
from __future__ import annotations

import json

import psycopg2
import pytest

from guardian.accounts import (
    CONTACT_FIELDS,
    NoSuchAccount,
    account_for_user,
    accounts_for_user,
    count_live_accounts,
    scrub_and_delete,
    set_status,
    update_contacts,
)
from tests.guardian.conftest import TEST_USER_ID, make_account, make_user

ACTOR = f"user:{TEST_USER_ID}"


def _poll_state(db, account_id, last_success_at=None):
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_poll_state (account_id, last_success_at) VALUES (%s, %s)",
                    (account_id, last_success_at))


def _raw(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _events(db, account_id):
    with db.cursor() as cur:
        cur.execute("SELECT event_type, severity, payload_json, actor FROM guardian_events WHERE account_id = %s ORDER BY id",
                    (account_id,))
        return [dict(r) for r in cur.fetchall()]


def _engine_would_poll(db):
    """The predicate the engine uses to pick accounts (guardian/engine.py)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM guardian_accounts WHERE status = 'active' AND baseline_status <> 'seeding' ORDER BY id")
        return [r["id"] for r in cur.fetchall()]


# --- listing: whose rows a customer sees -------------------------------------


def test_accounts_for_user_lists_only_the_owners_rows_with_their_poll_state(db):
    mine = make_account(db, "mine-1")
    mine_later = make_account(db, "mine-2")
    _poll_state(db, mine, last_success_at="2026-03-01T10:00:00+00:00")
    other = make_user(db, "someone-else@example.com")
    make_account(db, "theirs", user_id=other)

    rows = accounts_for_user(db, TEST_USER_ID)

    assert [r["id"] for r in rows] == [mine, mine_later]
    assert rows[0]["last_success_at"] == "2026-03-01T10:00:00+00:00"
    assert rows[1]["last_success_at"] is None          # no poll_state row yet: LEFT JOIN, not an error
    assert {r["label"] for r in rows} == {"mine-1", "mine-2"}


def test_accounts_for_user_with_no_owner_or_no_rows_is_empty(db):
    assert accounts_for_user(db, None) == []
    assert accounts_for_user(db, make_user(db, "nobody-yet@example.com")) == []


def test_account_for_user_returns_none_for_somebody_elses_row(db):
    """Not-yours and not-there are the same answer, so the caller can turn
    either into 404 and the response tells nobody which it was."""
    other = make_user(db, "someone-else@example.com")
    theirs = make_account(db, "theirs", user_id=other)
    mine = make_account(db, "mine")

    assert account_for_user(db, TEST_USER_ID, theirs) is None
    assert account_for_user(db, TEST_USER_ID, 424242) is None
    assert account_for_user(db, None, mine) is None
    assert account_for_user(db, TEST_USER_ID, None) is None
    found = account_for_user(db, TEST_USER_ID, mine)
    assert found is not None and found["id"] == mine and found["label"] == "mine"
    assert "last_success_at" in found


# --- contacts -------------------------------------------------------------------


def test_update_contacts_changes_the_fields_and_records_their_names_but_not_their_values(db):
    account_id = make_account(db, "contacts")

    row = update_contacts(db, account_id, actor=ACTOR, alert_phone="+1 555 000 1111",
                          alert_email_secondary="  Second@Example.com ", telegram_handle="@alice")

    assert row["alert_phone"] == "+1 555 000 1111"
    assert row["alert_email_secondary"] == "second@example.com"
    assert row["telegram_handle"] == "@alice"
    events = _events(db, account_id)
    assert [e["event_type"] for e in events] == ["alert_contact_updated"]
    payload = json.loads(events[0]["payload_json"])
    assert payload == {"fields": ["alert_email_secondary", "alert_phone", "telegram_handle"]}
    for fragment in ("555", "example.com", "alice"):
        assert fragment not in events[0]["payload_json"]
    assert events[0]["actor"] == ACTOR


def test_update_contacts_records_only_what_changed_and_nothing_when_nothing_did(db):
    account_id = make_account(db, "contacts")
    update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550001111")

    same = update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550001111", telegram_handle=None)
    assert same["alert_phone"] == "+15550001111"
    assert len(_events(db, account_id)) == 1, "an unchanged value is not a change"

    update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550001111", alert_phone_secondary="+15550002222")
    events = _events(db, account_id)
    assert len(events) == 2
    assert json.loads(events[-1]["payload_json"]) == {"fields": ["alert_phone_secondary"]}


def test_update_contacts_clears_a_field_with_a_blank_and_toggles_sms(db):
    account_id = make_account(db, "contacts")
    update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550001111")

    row = update_contacts(db, account_id, actor=ACTOR, alert_phone="   ", sms_alerts_enabled=False)

    assert row["alert_phone"] is None
    assert row["sms_alerts_enabled"] is False
    assert json.loads(_events(db, account_id)[-1]["payload_json"]) == {"fields": ["alert_phone", "sms_alerts_enabled"]}


def test_update_contacts_bumps_updated_at(db):
    account_id = make_account(db, "contacts")
    before = _raw(db, account_id)["updated_at"]
    update_contacts(db, account_id, actor=ACTOR, telegram_handle="@bob")
    assert _raw(db, account_id)["updated_at"] != before


@pytest.mark.parametrize("field,value", [
    ("alert_phone", "+" + "5" * 40),
    ("alert_email_secondary", "a" * 250 + "@example.com"),
    ("alert_phone_secondary", "+" + "5" * 40),
    ("telegram_handle", "@" + "x" * 70),
])
def test_update_contacts_refuses_a_value_that_does_not_fit_in_plain_words(db, field, value):
    account_id = make_account(db, "contacts")
    with pytest.raises(ValueError) as info:
        update_contacts(db, account_id, actor=ACTOR, **{field: value})
    assert "too long" in str(info.value)
    assert _events(db, account_id) == []


def test_update_contacts_refuses_unknown_fields_and_non_boolean_sms(db):
    account_id = make_account(db, "contacts")
    with pytest.raises(ValueError, match="unknown contact field"):
        update_contacts(db, account_id, actor=ACTOR, encrypted_api_key="x")
    with pytest.raises(ValueError, match="unknown contact field"):
        update_contacts(db, account_id, actor=ACTOR, label="renamed")
    with pytest.raises(ValueError):
        update_contacts(db, account_id, actor=ACTOR, sms_alerts_enabled="false")
    assert _raw(db, account_id)["encrypted_api_key"] == "x"
    assert set(CONTACT_FIELDS) == {"alert_phone", "alert_email_secondary", "alert_phone_secondary",
                                   "telegram_handle", "sms_alerts_enabled"}


def test_update_contacts_on_a_missing_row_is_a_programming_error(db):
    with pytest.raises(NoSuchAccount):
        update_contacts(db, 424242, actor=ACTOR, alert_phone="+15550001111")


# --- on and off ------------------------------------------------------------------


def test_disable_stops_the_engine_picking_the_account_and_is_recorded(db):
    account_id = make_account(db, "switch")
    assert _engine_would_poll(db) == [account_id]

    row = set_status(db, account_id, "disabled", actor=ACTOR)

    assert row["status"] == "disabled"
    assert _engine_would_poll(db) == []
    events = _events(db, account_id)
    assert [e["event_type"] for e in events] == ["account_disabled"]
    assert events[0]["severity"] == "HIGH"
    assert json.loads(events[0]["payload_json"]) == {"from": "active", "to": "disabled"}
    assert events[0]["actor"] == ACTOR


def test_enable_makes_the_account_eligible_again_and_is_recorded(db):
    account_id = make_account(db, "switch")
    set_status(db, account_id, "disabled", actor=ACTOR)

    row = set_status(db, account_id, "active", actor=ACTOR)

    assert row["status"] == "active"
    assert _engine_would_poll(db) == [account_id]
    assert [e["event_type"] for e in _events(db, account_id)] == ["account_disabled", "account_enabled"]


def test_setting_the_status_the_row_already_has_records_nothing(db):
    account_id = make_account(db, "switch")
    row = set_status(db, account_id, "active", actor=ACTOR)
    assert row["status"] == "active"
    assert _events(db, account_id) == []


def test_a_customer_may_only_set_active_or_disabled(db):
    account_id = make_account(db, "switch")
    for status in ("tamper_suspected", "deleted", "", None):
        with pytest.raises(ValueError):
            set_status(db, account_id, status, actor=ACTOR)
    assert _raw(db, account_id)["status"] == "active"
    with pytest.raises(NoSuchAccount):
        set_status(db, 424242, "disabled", actor=ACTOR)


def test_a_tamper_suspected_account_can_be_switched_back_on_by_its_owner(db):
    """The engine parks an account it cannot authenticate; once the customer
    has sorted the key out they turn it back on."""
    account_id = make_account(db, "switch")
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET status = 'tamper_suspected' WHERE id = %s", (account_id,))

    row = set_status(db, account_id, "active", actor=ACTOR)

    assert row["status"] == "active"
    assert json.loads(_events(db, account_id)[-1]["payload_json"]) == {"from": "tamper_suspected", "to": "active"}


def _set_uid(db, account_id, uid):
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET exchange_uid = %s WHERE id = %s", (uid, account_id))


def test_enable_is_refused_while_another_row_watches_the_same_exchange_account(db):
    """disable -> add the account again with a new key -> enable the old row
    would leave two active rows polling one exchange account. Onboarding
    refuses the second key while the first is live; enabling refuses the
    mirror image, and the message names the row that is watching."""
    old = make_account(db, "old-key")
    _set_uid(db, old, "12345")
    set_status(db, old, "disabled", actor=ACTOR)
    new = make_account(db, "new-key")
    _set_uid(db, new, "12345")

    with pytest.raises(ValueError) as info:
        set_status(db, old, "active", actor=ACTOR)

    assert "new-key" in str(info.value) and "already being watched" in str(info.value)
    assert _raw(db, old)["status"] == "disabled"
    assert [e["event_type"] for e in _events(db, old)] == ["account_disabled"], "a refused change is not recorded"
    assert _engine_would_poll(db) == [new]

    # Once the newer row is switched off, the old one may come back.
    set_status(db, new, "disabled", actor=ACTOR)
    assert set_status(db, old, "active", actor=ACTOR)["status"] == "active"
    assert _engine_would_poll(db) == [old]


def test_a_parked_row_also_blocks_enabling_its_twin(db):
    """`tamper_suspected` is not switched off: the engine parked it and the
    customer is expected to sort that key out - the same rule onboarding
    applies when it refuses a second key."""
    old = make_account(db, "old-key")
    _set_uid(db, old, "12345")
    set_status(db, old, "disabled", actor=ACTOR)
    parked = make_account(db, "parked")
    _set_uid(db, parked, "12345")
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET status = 'tamper_suspected' WHERE id = %s", (parked,))

    with pytest.raises(ValueError, match="parked"):
        set_status(db, old, "active", actor=ACTOR)
    assert _raw(db, old)["status"] == "disabled"


def test_enable_is_allowed_when_no_other_live_row_is_the_same_exchange_account(db):
    """Another owner's row, another exchange, a disabled twin and a row with no
    exchange id on record (Binance) are not duplicates - as at onboarding."""
    mine = make_account(db, "mine")
    _set_uid(db, mine, "12345")
    set_status(db, mine, "disabled", actor=ACTOR)
    other = make_user(db, "someone-else@example.com")
    theirs = make_account(db, "theirs", user_id=other)
    _set_uid(db, theirs, "12345")
    bybit = make_account(db, "bybit", exchange="bybit")
    _set_uid(db, bybit, "12345")
    twin = make_account(db, "twin")
    _set_uid(db, twin, "12345")
    set_status(db, twin, "disabled", actor=ACTOR)

    assert set_status(db, mine, "active", actor=ACTOR)["status"] == "active"

    unknown = make_account(db, "no-uid")
    set_status(db, unknown, "disabled", actor=ACTOR)
    assert _raw(db, unknown)["exchange_uid"] is None
    assert set_status(db, unknown, "active", actor=ACTOR)["status"] == "active"
    assert sorted(_engine_would_poll(db)) == sorted([mine, theirs, bybit, unknown])


# --- a change and its record are one transaction ------------------------------------


def _break_the_event_log(monkeypatch):
    """The event INSERT fails on the caller's cursor, as a full disk or a lost
    connection would make it fail: a statement that violates NOT NULL puts the
    transaction into an error state, and the UPDATE before it rolls back with it."""
    import guardian.accounts as accounts_module

    def refuse(db, account_id, *args, cur=None, **kwargs):
        assert cur is not None, "the event must be written on the change's own cursor"
        cur.execute("INSERT INTO guardian_events (account_id) VALUES (%s)", (account_id,))

    monkeypatch.setattr(accounts_module, "append_event", refuse)


def test_a_status_change_that_cannot_be_recorded_is_not_made(db, monkeypatch):
    """"Who touched my account" is only an honest page if every change on it
    is there. The UPDATE and its event share one transaction: when the event
    cannot be written, the row does not change either."""
    account_id = make_account(db, "atomic")
    _break_the_event_log(monkeypatch)

    with pytest.raises(psycopg2.Error):
        set_status(db, account_id, "disabled", actor=ACTOR)

    assert _raw(db, account_id)["status"] == "active"
    assert _events(db, account_id) == []
    assert _engine_would_poll(db) == [account_id]


def test_a_contact_change_or_a_removal_that_cannot_be_recorded_is_not_made(db, monkeypatch):
    account_id = make_account(db, "atomic")
    _break_the_event_log(monkeypatch)

    with pytest.raises(psycopg2.Error):
        update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550001111")
    with pytest.raises(psycopg2.Error):
        scrub_and_delete(db, account_id, actor=ACTOR)

    row = _raw(db, account_id)
    assert row["alert_phone"] is None
    assert row["status"] == "active" and row["encrypted_api_key"] == "x", "nothing was removed"
    assert _events(db, account_id) == []


def test_a_recorded_change_is_one_that_happened(db):
    """The other half: the event's row id comes back from the same transaction
    as the change, so an event in the log always has its change beside it."""
    account_id = make_account(db, "atomic")
    set_status(db, account_id, "disabled", actor=ACTOR)
    update_contacts(db, account_id, actor=ACTOR, telegram_handle="@carol")
    row = _raw(db, account_id)
    events = _events(db, account_id)
    assert row["status"] == "disabled" and row["telegram_handle"] == "@carol"
    assert [e["event_type"] for e in events] == ["account_disabled", "alert_contact_updated"]


# --- removal ------------------------------------------------------------------------


def test_scrub_and_delete_removes_the_key_material_and_keeps_the_row_and_its_history(db):
    account_id = make_account(db, "gone")
    _poll_state(db, account_id)
    update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550001111")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_alerts (account_id, rule, severity, dedup_key, message, evidence_json, confidence, sent_at) "
            "VALUES (%s, 'withdrawal_created', 'CRITICAL', 'k', 'm', '{}', 'high', '2026-03-01T00:00:00+00:00')",
            (account_id,),
        )
    history_before = _events(db, account_id)
    assert _raw(db, account_id)["key_fingerprint"] is None, "make_account stores no fingerprint; this test sets one"
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET key_fingerprint = %s WHERE id = %s", ("f" * 64, account_id))
    assert _raw(db, account_id)["key_fingerprint"] == "f" * 64

    row = scrub_and_delete(db, account_id, actor=ACTOR)

    assert row["status"] == "disabled"
    assert row["encrypted_api_key"] == "" and row["encrypted_secret"] == "" and row["encrypted_passphrase"] == ""
    assert row["key_fingerprint"] is None
    raw = _raw(db, account_id)
    assert raw is not None, "the row is kept"
    assert raw["label"] == "gone" and raw["alert_phone"] == "+15550001111"
    events = _events(db, account_id)
    assert events[:len(history_before)] == history_before, "earlier events untouched"
    assert events[-1]["event_type"] == "account_deleted"
    assert events[-1]["severity"] == "HIGH"
    assert json.loads(events[-1]["payload_json"]) == {"label": "gone", "exchange": "bitget", "previous_status": "active"}
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM guardian_alerts WHERE account_id = %s", (account_id,))
        assert cur.fetchone()["n"] == 1, "the alert history is kept"
    assert _engine_would_poll(db) == []


def test_a_removed_account_is_no_longer_the_customers_to_see_or_switch_on(db):
    account_id = make_account(db, "gone")
    kept = make_account(db, "kept")
    scrub_and_delete(db, account_id, actor=ACTOR)

    assert [r["id"] for r in accounts_for_user(db, TEST_USER_ID)] == [kept]
    assert account_for_user(db, TEST_USER_ID, account_id) is None
    with pytest.raises(ValueError, match="removed"):
        set_status(db, account_id, "active", actor=ACTOR)
    assert _raw(db, account_id)["status"] == "disabled"


def test_removing_twice_records_one_deletion(db):
    account_id = make_account(db, "gone")
    scrub_and_delete(db, account_id, actor=ACTOR)
    again = scrub_and_delete(db, account_id, actor=ACTOR)
    assert again["status"] == "disabled" and again["encrypted_api_key"] == ""
    assert [e["event_type"] for e in _events(db, account_id)] == ["account_deleted"]
    with pytest.raises(NoSuchAccount):
        scrub_and_delete(db, 424242, actor=ACTOR)


def test_no_contact_value_or_key_reaches_the_event_log_through_any_path(db):
    account_id = make_account(db, "quiet")
    update_contacts(db, account_id, actor=ACTOR, alert_phone="+15550009999", telegram_handle="@secretive")
    set_status(db, account_id, "disabled", actor=ACTOR)
    set_status(db, account_id, "active", actor=ACTOR)
    scrub_and_delete(db, account_id, actor=ACTOR)

    haystack = json.dumps(_events(db, account_id))
    for fragment in ("5550009999", "secretive", "x", "y", "z"):
        # 'x'/'y'/'z' are make_account's stand-in ciphertexts; they would only
        # appear as whole values, so check them as JSON strings.
        needle = f'"{fragment}"' if len(fragment) == 1 else fragment
        assert needle not in haystack, fragment


# --- the cap -----------------------------------------------------------------------


def test_count_live_accounts_counts_rows_that_still_hold_a_key(db):
    assert count_live_accounts(db, TEST_USER_ID) == 0
    assert count_live_accounts(db, None) == 0
    a = make_account(db, "a")
    b = make_account(db, "b")
    c = make_account(db, "c")
    other = make_user(db, "someone-else@example.com")
    make_account(db, "theirs", user_id=other)
    assert count_live_accounts(db, TEST_USER_ID) == 3

    set_status(db, b, "disabled", actor=ACTOR)
    assert count_live_accounts(db, TEST_USER_ID) == 3, "disabled still holds a key and still counts"

    scrub_and_delete(db, c, actor=ACTOR)
    assert count_live_accounts(db, TEST_USER_ID) == 2, "a removed account no longer counts"
    assert count_live_accounts(db, other) == 1
    assert account_for_user(db, TEST_USER_ID, a) is not None
