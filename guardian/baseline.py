from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from guardian.db import Database
from guardian.exchange import ExchangeClient, exchange_account_uid
from guardian.identity import fill_identity, transfer_identity, withdrawal_identity
from guardian.numeric import plain, to_decimal

logger = logging.getLogger("guardian.baseline")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_known_address(db: Database, account_id: int, address: str | None, coin: str | None, source: str) -> bool:
    """Returns True if the address was newly added to the baseline."""
    if not address:
        return False
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO guardian_known_addresses (account_id, address, coin, source, first_seen_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(account_id, address) DO NOTHING
            """,
            (account_id, address, coin, source, _now()),
        )
        return cur.rowcount == 1


def is_known_address(db: Database, account_id: int, address: str) -> bool:
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM guardian_known_addresses WHERE account_id = %s AND address = %s",
            (account_id, address),
        )
        return cur.fetchone() is not None


def record_seen_withdrawal(db: Database, account_id: int, record: dict) -> bool:
    """Records a withdrawal (CCXT unified transaction dict) if not already
    seen. Returns True if it was newly recorded, False if already known -
    the poller uses that to decide whether to fire the withdrawal-created
    detection.

    A record the exchange sent without any reference used to return False here,
    which the poller reads as "already seen" - so it never reached the
    withdrawal-created or unseen-address detectors, and money left the account
    with nobody told (G-11). `withdrawal_identity` derives a key from the fields
    that don't move as the withdrawal settles, so an unreferenced withdrawal is
    alerted once rather than never."""
    order_id = withdrawal_identity(record)
    status = str(record.get("status") or "unknown")
    coin = record.get("currency")
    size = str(record.get("amount"))
    address = record.get("address")
    with db.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM guardian_seen_withdrawals WHERE account_id = %s AND order_id = %s",
            (account_id, order_id),
        )
        if cur.fetchone() is not None:
            return False
        cur.execute(
            """
            INSERT INTO guardian_seen_withdrawals
                (account_id, order_id, status, coin, size, address, raw_json, first_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (account_id, order_id, status, coin, size, address, json.dumps(record, default=str), _now()),
        )
    return True


def record_balance_snapshot(db: Database, account_id: int, balances: dict[str, object]) -> None:
    now = _now()
    with db.cursor() as cur:
        for coin, qty in balances.items():
            cur.execute(
                "INSERT INTO guardian_balance_snapshots (account_id, coin, quantity, snapshot_at) VALUES (%s, %s, %s, %s)",
                (account_id, coin, str(to_decimal(qty)), now),
            )


def latest_balance_snapshot(db: Database, account_id: int) -> dict[str, object]:
    """Most recent quantity per coin, as Decimal."""
    balances, _ = latest_balance_snapshot_at(db, account_id)
    return balances


def latest_balance_snapshot_at(db: Database, account_id: int) -> tuple[dict[str, object], str | None]:
    """Most recent quantity per coin, plus the ISO timestamp that snapshot was
    taken at (None when the account has no snapshot yet).

    The timestamp is what bounds the quantity-drop explanation window: a drop
    is measured from this snapshot, so only activity at or after it can
    explain that drop. `last_fast_loop_at` is NOT a safe substitute - it is
    written near the top of the fast loop while the snapshot is written at the
    bottom, and onboarding's seed_baseline records a snapshot without any poll
    state at all."""
    with db.cursor() as cur:
        # The MAX() subquery must be non-correlated (parameterized, not
        # b.account_id): correlating it re-evaluates the MAX per row, which is
        # O(n^2) on a table that grows every fast loop - it wedged the live
        # dashboard within ~10 hours of polling (2026-07-06 outage).
        cur.execute(
            """
            SELECT coin, quantity, snapshot_at FROM guardian_balance_snapshots
            WHERE account_id = %s AND snapshot_at = (
                SELECT MAX(snapshot_at) FROM guardian_balance_snapshots WHERE account_id = %s
            )
            """,
            (account_id, account_id),
        )
        rows = cur.fetchall()
    # `plain` takes off the padding numeric(38,18) adds on the way back
    # (Decimal("1.000000000000000000"), Decimal("0E-18")). The value is
    # untouched, so the detectors compute exactly what they did; what changes
    # is how a drop reads in the alert text they build from these.
    balances = {row["coin"]: plain(to_decimal(row["quantity"])) for row in rows}
    snapshot_at = rows[0]["snapshot_at"] if rows else None
    return balances, snapshot_at


def prune_balance_snapshots(db: Database, account_id: int, keep_days: int = 7) -> int:
    """Deletes snapshots older than the retention window. Snapshots exist to
    answer 'what changed since the last poll?' - week-old rows only slow that
    down. Returns the number of rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM guardian_balance_snapshots WHERE account_id = %s AND snapshot_at < %s",
            (account_id, cutoff),
        )
        return cur.rowcount


def record_permission_snapshot(db: Database, account_id: int, authorities: object, ip_whitelist: object) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_permission_snapshots (account_id, authorities, ip_whitelist, snapshot_at) "
            "VALUES (%s, %s, %s, %s)",
            (account_id, json.dumps(authorities, default=str), json.dumps(ip_whitelist, default=str), _now()),
        )


def learn_exchange_uid(db: Database, account_id: int, uid: str | None) -> str:
    """Record which exchange account this key belongs to, once (#69).

    Returns what happened, in one word, so a caller can act on it: `learned`,
    `unchanged`, `unknown` (the exchange did not tell us), or `mismatch`.

    **A recorded uid is never overwritten.** It is the thing a replacement key
    is checked against, so a path that quietly updates it would let the check
    it exists for be walked past one poll at a time. `mismatch` therefore
    reports rather than writes: the same stored key answering with a different
    account id is either an exchange changing its response shape or something
    that deserves a person, and neither is fixed by believing the new value."""
    if uid is None or not str(uid).strip():
        return "unknown"
    uid = str(uid).strip()
    with db.cursor() as cur:
        cur.execute("SELECT exchange_uid FROM guardian_accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
        if row is None:
            return "unknown"
        current = (row["exchange_uid"] or "").strip()
        if current:
            return "unchanged" if current == uid else "mismatch"
        cur.execute("UPDATE guardian_accounts SET exchange_uid = %s WHERE id = %s", (uid, account_id))
    return "learned"


def latest_permission_snapshot(db: Database, account_id: int) -> dict | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT authorities, ip_whitelist FROM guardian_permission_snapshots "
            "WHERE account_id = %s ORDER BY snapshot_at DESC LIMIT 1",
            (account_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "authorities": json.loads(row["authorities"]) if row["authorities"] else None,
        "ip_whitelist": json.loads(row["ip_whitelist"]) if row["ip_whitelist"] else None,
    }


def record_traded_pair(db: Database, account_id: int, symbol: str) -> bool:
    """Returns True if this symbol is new to the account's trading baseline."""
    if not symbol:
        return False
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_traded_pairs (account_id, symbol, first_seen_at) VALUES (%s, %s, %s) "
            "ON CONFLICT(account_id, symbol) DO NOTHING",
            (account_id, symbol, _now()),
        )
        return cur.rowcount == 1


def known_traded_pairs(db: Database, account_id: int) -> set[str]:
    with db.cursor() as cur:
        cur.execute("SELECT symbol FROM guardian_traded_pairs WHERE account_id = %s", (account_id,))
        return {r["symbol"] for r in cur.fetchall()}


def record_seen_fill(db: Database, account_id: int, fill: dict) -> bool:
    """Returns True the first time this fill is offered, False every time
    after. The poller uses it to price-check and fee-check a fill once,
    whether or not the exchange dated it."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_seen_fills (account_id, fill_key, symbol, first_seen_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(account_id, fill_key) DO NOTHING",
            (account_id, fill_identity(fill), fill.get("symbol"), _now()),
        )
        return cur.rowcount == 1


def record_seen_transfer(db: Database, account_id: int, transfer: dict) -> bool:
    """Returns True if this transfer record hasn't been processed before.

    A transfer with no `id` used to return False here, which the poller reads as
    "already seen" - so it never reached `detect_new_transfer_route`. That is
    G-11's defect exactly, in the function G-11 didn't touch."""
    transfer_id = transfer_identity(transfer)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_seen_transfers (account_id, transfer_id, raw_json, first_seen_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(account_id, transfer_id) DO NOTHING",
            (account_id, transfer_id, json.dumps(transfer, default=str), _now()),
        )
        return cur.rowcount == 1


def record_transfer_route(db: Database, account_id: int, route: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_transfer_routes (account_id, route, first_seen_at) VALUES (%s, %s, %s) "
            "ON CONFLICT(account_id, route) DO NOTHING",
            (account_id, route, _now()),
        )


def known_transfer_routes(db: Database, account_id: int) -> set[str]:
    with db.cursor() as cur:
        cur.execute("SELECT route FROM guardian_transfer_routes WHERE account_id = %s", (account_id,))
        return {r["route"] for r in cur.fetchall()}


def load_fee_baseline(db: Database, account_id: int) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT ema_json FROM guardian_fee_baseline WHERE account_id = %s", (account_id,))
        row = cur.fetchone()
    if row is None:
        return {}
    return {coin: to_decimal(v) for coin, v in json.loads(row["ema_json"]).items()}


def save_fee_baseline(db: Database, account_id: int, ema: dict) -> None:
    payload = json.dumps({coin: str(v) for coin, v in ema.items()})
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO guardian_fee_baseline (account_id, ema_json, updated_at) VALUES (%s, %s, %s) "
            "ON CONFLICT(account_id) DO UPDATE SET ema_json = excluded.ema_json, updated_at = excluded.updated_at",
            (account_id, payload, _now()),
        )


def seed_baseline(db: Database, account_id: int, client: ExchangeClient) -> list[dict]:
    """Called once at onboarding. Builds the initial 'known good' picture so
    the first fast-loop poll doesn't fire CRITICAL alerts for history that
    predates monitoring (pre-existing withdrawals/addresses are baseline,
    not anomalies).

    Returns the seeds that FAILED, as [{"what": ..., "error": ...}]. The
    seeding is best-effort on purpose - a broken order-history endpoint should
    not block onboarding, and that decision is unchanged. But the consequence
    lands later and on the customer: an account seeded without its trading
    history false-positives on `never_touched_pair_activity` for pairs they
    have always traded, and until now nobody could tell that account apart from
    one that started complete (#58, group C).

    Returning the gaps rather than paging from here keeps this function free of
    the alerting stack, which onboarding may not have constructed."""
    balance = client.fetch_balance()
    totals = balance.get("total", {}) if isinstance(balance, dict) else {}
    record_balance_snapshot(db, account_id, {k: v for k, v in totals.items() if v})

    for record in client.fetch_deposits():
        record_known_address(db, account_id, record.get("address"), record.get("currency"), "deposit_history")

    for record in client.fetch_withdrawals():
        record_known_address(db, account_id, record.get("address"), record.get("currency"), "withdrawal_history")
        record_seen_withdrawal(db, account_id, record)

    # Seed the trading-pair baseline from order history so active traders
    # (incl. AI futures accounts) don't false-positive on day one.
    gaps: list[dict] = []
    try:
        for order in client.fetch_closed_orders():
            record_traded_pair(db, account_id, order.get("symbol"))
    except Exception as exc:  # noqa: BLE001 - order history is a best-effort seed
        logger.warning("account %d: closed-order seeding failed: %s", account_id, exc)
        gaps.append({"what": "closed_orders", "error": str(exc),
                     "consequence": "never_touched_pair_activity may false-positive on "
                                    "pairs this customer has always traded"})

    # Seed transfer history the same way: internal routes the customer already
    # uses (spot->funding, spot->p2p, ...) are baseline, not first-poll HIGH
    # alerts. Seeds twice the poller's default sweep width (detectors.transfers
    # .max_coins_per_cycle, default 5) so a modest raise of that knob doesn't
    # expose unseeded coins whose old transfers would false-alert.
    nonzero = {k: v for k, v in totals.items() if v}
    top_coins = sorted(nonzero, key=lambda c: to_decimal(nonzero[c]), reverse=True)[:10]
    for coin in top_coins:
        try:
            for transfer in client.fetch_transfers(coin):
                record_seen_transfer(db, account_id, transfer)
                route = f"{transfer.get('fromAccount') or '?'}->{transfer.get('toAccount') or '?'}"
                record_transfer_route(db, account_id, route)
        except Exception as exc:  # noqa: BLE001 - transfer history is a best-effort seed
            logger.warning("account %d: transfer seeding failed for %s: %s", account_id, coin, exc)
            gaps.append({"what": "transfers", "coin": coin, "error": str(exc),
                         "consequence": "new_transfer_route may false-positive on routes this "
                                        "customer already uses"})
            continue

    info = client.fetch_account_info()
    payload = info.get("data", info) if isinstance(info, dict) else info
    if isinstance(payload, dict):
        record_permission_snapshot(db, account_id, payload.get("authorities"), payload.get("ips"))
        # Learned here rather than fetched separately: this payload is the one
        # that carries it, and onboarding is the moment we know the key and the
        # account genuinely belong together, because a person put them there.
        learn_exchange_uid(db, account_id, exchange_account_uid(client.exchange_id, payload))

    return gaps
