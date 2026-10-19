"""guardian/db.py: the SQLite contract on Postgres.

The engine reads timestamps as ISO text, JSON as text, quantities as Decimal,
and `cur.lastrowid` after a plain INSERT. Each of those is a typecaster or a
shim here, and each is a test."""
import json
from decimal import Decimal

import pytest

from tests.guardian.conftest import TEST_USER_ID, make_account


def test_timestamps_come_back_as_iso_text_the_engine_can_fromisoformat(db):
    from datetime import datetime
    account_id = make_account(db)
    with db.cursor() as cur:
        cur.execute("SELECT onboarded_at FROM guardian_accounts WHERE id = %s", (account_id,))
        value = cur.fetchone()["onboarded_at"]
    assert isinstance(value, str) and value.startswith("2026-01-01T00:00:00")
    assert datetime.fromisoformat(value).year == 2026


def test_json_columns_come_back_as_text_the_engine_json_loads(db):
    account_id = make_account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                    "VALUES (%s, 'x', 'LOW', %s, '2026-01-02T03:04:05+00:00')",
                    (account_id, json.dumps({"amount": str(Decimal("1.5")), "n": 2})))
        cur.execute("SELECT payload_json, created_at FROM guardian_events WHERE account_id = %s", (account_id,))
        row = cur.fetchone()
    assert isinstance(row["payload_json"], str)
    assert json.loads(row["payload_json"]) == {"amount": "1.5", "n": 2}
    assert row["created_at"] == "2026-01-02T03:04:05+00:00"


def test_numeric_quantities_come_back_as_decimal(db):
    account_id = make_account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_balance_snapshots (account_id, coin, quantity, snapshot_at) "
                    "VALUES (%s, 'BTC', %s, '2026-01-01')", (account_id, "0.123456789012345678"))
        cur.execute("SELECT quantity FROM guardian_balance_snapshots WHERE account_id = %s", (account_id,))
        value = cur.fetchone()["quantity"]
    assert isinstance(value, Decimal) and value == Decimal("0.123456789012345678")


def test_lastrowid_works_for_a_plain_insert_and_returning_is_left_alone(db):
    account_id = make_account(db)
    assert isinstance(account_id, int) and account_id >= 1
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                    "VALUES (%s, 'x', 'LOW', '{}', '2026-01-01') RETURNING id", (account_id,))
        explicit = cur.fetchone()["id"]
        assert cur.lastrowid is None, "a statement that says RETURNING is the caller's to fetch"
    assert isinstance(explicit, int)


def test_rowcount_is_zero_on_a_conflict_that_did_nothing(db):
    account_id = make_account(db)
    with db.cursor() as cur:
        cur.execute("INSERT INTO guardian_known_addresses (account_id, address, source, first_seen_at) "
                    "VALUES (%s, 'addr', 'seed', '2026-01-01')", (account_id,))
        cur.execute("INSERT INTO guardian_known_addresses (account_id, address, source, first_seen_at) "
                    "VALUES (%s, 'addr', 'seed', '2026-01-01') ON CONFLICT (account_id, address) DO NOTHING", (account_id,))
        assert cur.rowcount == 0


def test_uuid_parameters_round_trip(db):
    account_id = make_account(db)
    with db.cursor() as cur:
        cur.execute("SELECT user_id FROM guardian_accounts WHERE id = %s", (account_id,))
        assert cur.fetchone()["user_id"] == TEST_USER_ID


def test_an_exception_inside_the_cursor_rolls_the_transaction_back(db):
    account_id = make_account(db)
    with pytest.raises(RuntimeError):
        with db.cursor() as cur:
            cur.execute("INSERT INTO guardian_events (account_id, event_type, severity, payload_json, created_at) "
                        "VALUES (%s, 'x', 'LOW', '{}', '2026-01-01')", (account_id,))
            raise RuntimeError("boom")
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM guardian_events WHERE account_id = %s", (account_id,))
        assert cur.fetchone()["n"] == 0


def test_rows_support_the_sqlite_row_api(db):
    account_id = make_account(db, "acct-row")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    assert row["label"] == "acct-row" and "exchange" in row.keys() and dict(row)["topology"] == "independent"
