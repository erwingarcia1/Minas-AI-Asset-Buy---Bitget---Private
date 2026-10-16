from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from guardian.alerts.dispatch import dispatch
from guardian.baseline import (
    known_traded_pairs,
    known_transfer_routes,
    latest_balance_snapshot,
    latest_balance_snapshot_at,
    latest_permission_snapshot,
    learn_exchange_uid,
    load_fee_baseline,
    prune_balance_snapshots,
    record_balance_snapshot,
    record_known_address,
    record_permission_snapshot,
    record_seen_fill,
    record_seen_transfer,
    record_seen_withdrawal,
    record_traded_pair,
    record_transfer_route,
    save_fee_baseline,
)
from guardian.config import RulesConfig
from guardian.crypto import CredentialCipher
from guardian.db import Database
from guardian.detectors.burst import detect_withdrawal_burst
from guardian.detectors.fee_anomaly import detect_fee_anomaly, sum_fees_by_coin, update_ema
from guardian.detectors.heartbeat import detect_baseline_drift, detect_unreachable
from guardian.detectors.new_api_key import detect_permission_state_change
from guardian.detectors.new_pair import detect_new_pair_activity
from guardian.detectors.off_market import detect_off_market_fills
from guardian.detectors.quantity_drop import detect_quantity_drop
from guardian.detectors.tamper import detect_tamper
from guardian.detectors.transfer_pattern import detect_new_transfer_route
from guardian.detectors.unseen_address import detect_unseen_address
from guardian.detectors.withdrawal import detect_new_withdrawals
from guardian.events import append_event
from guardian.exchange import ExchangeClient, TamperSignal, exchange_account_uid, make_client
from guardian.identity import is_unidentifiable
from guardian.numeric import to_decimal
from guardian.rules_schema import default_for

logger = logging.getLogger("guardian.poller")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _account_exchange(row) -> str:
    """The account's exchange, for metric labels.

    `exchange` may be absent on rows read from a pre-migration DB snapshot;
    default to bitget to match the historical single-exchange behavior. A label
    of `"unknown"` here would be worse than a slightly optimistic one - it would
    split the series an alarm binds to."""
    return (row["exchange"] if "exchange" in row.keys() else None) or "bitget"


def _load_client(cipher: CredentialCipher, row) -> ExchangeClient:
    return make_client(
        _account_exchange(row),
        cipher.decrypt(row["encrypted_api_key"]),
        cipher.decrypt(row["encrypted_secret"]),
        cipher.decrypt(row["encrypted_passphrase"]),
    )


def _fill_timestamp_ms(fill: dict, label: str) -> int | None:
    """The fill's exchange timestamp in ms, or None when it has none or the
    one it has can't be read.

    None means "cannot be placed in time" and never 0. The `or 0` this
    replaces made an undated fill look older than every window, which is how
    it stopped being price-checked at all (G-47)."""
    raw = fill.get("timestamp")
    if raw is None or raw == "":
        logger.warning(
            "%s: fill %s has no timestamp - price-checking it on first sighting instead",
            label,
            fill.get("id") or fill.get("symbol") or "<unidentified>",
        )
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s: fill %s has an unparseable timestamp %r - price-checking it on first sighting instead",
            label,
            fill.get("id") or fill.get("symbol") or "<unidentified>",
            raw,
        )
        return None


def _get_poll_state(db: Database, account_id: int) -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM guardian_poll_state WHERE account_id = %s", (account_id,))
        row = cur.fetchone()
    return dict(row) if row else {"consecutive_auth_failures": 0, "consecutive_failures": 0}


def _update_poll_state(db: Database, account_id: int, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with db.cursor() as cur:
        cur.execute(
            f"UPDATE guardian_poll_state SET {set_clause} WHERE account_id = %s",
            (*fields.values(), account_id),
        )


class AccountPoller:
    """Runs the fast (money movement + trading) and slow (config/security
    snapshot) loops for a single account. Driven either sequentially by the
    CLI's `run` command or concurrently by the worker pool in
    sentinel/engine.py; per-call rate limiting is handled by the engine via
    the optional `limiter`."""

    def __init__(self, db: Database, cipher: CredentialCipher, rules: RulesConfig, channels: dict, limiter=None, metrics=None, contact_router=None, operator_alerter=None):
        self._db = db
        self._cipher = cipher
        self._rules = rules
        self._channels = channels
        self._limiter = limiter
        self._metrics = metrics
        self._contact_router = contact_router
        self._operator_alerter = operator_alerter

    def _report_unidentifiable(self, account_id: int, label: str, account_row, withdrawals) -> None:
        """Pages when the exchange sends a withdrawal we cannot tell apart from
        another one (#67).

        The collision is not fixed here - the options for fixing it trade
        against the property that one settling withdrawal keeps one identity,
        and none should be chosen without knowing how often this happens. What
        is fixed is that it used to happen in complete silence: the second
        withdrawal was dropped as already seen and the poll reported success.

        Counted per record so the frequency is real, paged once per poll
        because the condition is about the exchange's response format and not
        about any one record. An exchange that has stopped sending references
        sends a whole page of them, and one page per record would bury the
        operator in the thing they are being told about."""
        unidentifiable = [w for w in withdrawals if is_unidentifiable(w)]
        if not unidentifiable:
            return

        exchange = (account_row["exchange"] if "exchange" in account_row.keys() else None) or "exchange"
        if self._metrics is not None:
            for _ in unidentifiable:
                self._metrics.unidentifiable_withdrawal(exchange)

        logger.warning(
            "%s: %d withdrawal(s) arrived with neither a reference nor a timestamp - "
            "these cannot be told apart, so all but the first are dropped as already seen",
            label, len(unidentifiable),
        )
        if self._operator_alerter is None:
            return
        self._operator_alerter.page(
            rule="withdrawal_cannot_be_identified",
            severity="HIGH",
            message=(
                f"{exchange} sent {len(unidentifiable)} withdrawal(s) on {label} with neither an "
                f"order id/txid nor a timestamp. Withdrawals like these cannot be told apart from "
                f"each other, so only the first is alerted on and any further identical ones are "
                f"dropped as already seen - a MISSED CRITICAL on the money-leaving path. This is "
                f"an exchange response-format problem; check the adapter and the exchange's API "
                f"changelog."
            ),
            evidence={
                "account_label": label,
                "exchange": exchange,
                "count": len(unidentifiable),
                "example": {k: unidentifiable[0].get(k)
                            for k in ("currency", "amount", "address", "network")},
            },
            account_id=account_id,
            dedup_scope=exchange,
        )

    def _account_contact(self, account_id: int) -> tuple[list[str], list[str]]:
        from guardian.contacts import account_contacts
        return account_contacts(self._db, account_id)

    def _emit(self, account_id: int, label: str, detection) -> None:
        append_event(self._db, account_id, detection.rule, detection.severity, detection.evidence)
        if self._metrics is not None:
            self._metrics.detection_fired(detection.rule, detection.severity)
        cooldowns = {
            sev: self._rules.get_int("alerting", "cooldown_minutes", sev)
            for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        }
        rule_cooldowns = {
            # Repeats daily while unresolved: silence must never look like
            # safety, and a half-hourly repeat is its own kind of silence.
            "account_unreachable": self._rules.get_int("alerting", "unreachable_repeat_minutes"),
        }
        alert_id = dispatch(
            self._db, self._channels, account_id, label, detection, cooldowns,
            rule_cooldowns=rule_cooldowns,
        )
        if alert_id is not None:
            if self._metrics is not None:
                self._metrics.alert_sent(detection.severity)
            self._page_if_undelivered(account_id, label, detection, alert_id)
            # Per-account destination fires only when the alert actually fired
            # (dedup/cooldown passed), reusing dispatch's dedup semantics. The
            # router is called even with no contacts on file so a CRITICAL
            # with no SMS destination is logged for follow-up (AC: missing
            # phone handling), and the alert id links sms_deliveries rows
            # back to the stored alert.
            if self._contact_router is not None:
                emails, phones = self._account_contact(account_id)
                self._contact_router.send(
                    detection, label, emails, phones,
                    account_id=account_id, alert_id=alert_id,
                )

    def _page_if_undelivered(self, account_id: int, label: str, detection, alert_id: int) -> None:
        """Pages an operator when an alert reached nobody.

        Not when *a* channel failed - that is degraded, and dispatch already
        tried the others. Only when the count is zero, which is the case where
        the customer is genuinely uninformed: every configured channel refused
        it, or none was configured for that severity at all. Paging on anything
        weaker would train an operator to ignore the channel, which costs more
        than it buys (#57).

        Two outcomes also write `delivered_to = 0` and are neither of those. A
        snoozed alert was withheld at the customer's request, and the row says
        so in `snoozed_until` (guardian/snooze.py: sharing one column would turn
        every snooze into a false report of a broken channel). A MEDIUM or LOW
        alert routed to the digest has no immediate channel by design - the
        daily digest delivers it from this same row. Reading only the count
        paged CRITICAL, operator SMS included, on every one of them."""
        if self._operator_alerter is None:
            return
        with self._db.cursor() as cur:
            cur.execute("SELECT delivered_to, snoozed_until FROM guardian_alerts WHERE id = %s", (alert_id,))
            row = cur.fetchone()
        if row is None or row["delivered_to"] > 0:
            return
        if row["snoozed_until"]:
            return
        if self._digest_bound(detection.severity):
            return
        self._operator_alerter.page(
            rule="alert_delivered_to_nobody",
            severity="CRITICAL",
            message=(
                f"A {detection.severity} alert ({detection.rule}) on {label} was detected and "
                f"reached no channel at all. It is recorded in the customer's alert history, "
                f"but nobody was told. Check the channels routed for {detection.severity}."
            ),
            evidence={
                "alert_id": alert_id,
                "rule": detection.rule,
                "severity": detection.severity,
                "account_label": label,
            },
            account_id=account_id,
            dedup_scope=detection.severity,
        )

    def _digest_bound(self, severity: str) -> bool:
        """Whether the daily digest carries this severity: `alerting.routing`
        lists `digest` for it. `build_channels` drops that entry when it
        resolves the immediate channels, so from here a digest-bound severity
        has the same shape as one with nothing configured - an empty list -
        and the routing is the only place the difference is still written
        down. Read the way `build_channels` reads it, defaults included."""
        routed = self._rules.get("alerting", "routing", severity, default=None)
        if not isinstance(routed, (list, tuple)) or not routed:
            routed = default_for("alerting", "routing", severity) or []
        return any(str(name) == "digest" for name in routed)

    def _acquire(self, account_id: int, calls: float) -> None:
        if self._limiter is not None:
            self._limiter.acquire_for_call(account_id, calls)

    def run_fast_loop(self, account_row) -> None:
        account_id = account_row["id"]
        label = account_row["label"]
        state = _get_poll_state(self._db, account_id)

        # Hints for per-symbol exchanges (Binance): pairs this account is known
        # to have traded, plus coins held on the previous poll - so a coin sold
        # to zero still gets its sell fills pulled and reconciled.
        pair_hints = sorted(known_traded_pairs(self._db, account_id))
        coin_hints = sorted(latest_balance_snapshot(self._db, account_id))

        def charge(calls: float = 1.0) -> None:
            # Rate-limiter hook for clients that fan one logical pull into
            # several HTTP calls (Bybit/OKX categories, Binance per-symbol) -
            # keeps the egress-IP budget honest about real request volume.
            self._acquire(account_id, calls)

        try:
            client = _load_client(self._cipher, account_row)
            self._acquire(account_id, 4)
            deposits = client.fetch_deposits()
            withdrawals = client.fetch_withdrawals()
            fills = client.fetch_closed_orders(symbol_hints=pair_hints, coin_hints=coin_hints, acquire=charge)
            balance = client.fetch_balance()
        except TamperSignal as exc:
            self._handle_auth_failure(account_id, label, state, exc)
            return
        except Exception as exc:  # noqa: BLE001 - one bad account must never stall the fleet
            self._handle_generic_failure(account_id, label, state, exc)
            return

        # Success is recorded at the *end* of this method, not here (#59).
        #
        # It used to be stamped the moment the fetches returned, with nine
        # detector invocations still to run, and `consecutive_failures` was
        # reset alongside it. So a cycle that died partway counted as a success,
        # never incremented the failure counter, never let `detect_unreachable`
        # fire, and left the account green on the dashboard with a
        # `last_success_at` of seconds ago - while nothing had been examined.
        # An account could sit like that indefinitely.
        #
        # `last_fast_loop_at` moves with it: it bounds which fills are still
        # worth price-checking against the current ticker, so advancing it for a
        # cycle that never reached the fill section would step the window over
        # fills nothing ever judged.
        try:
            self._run_fast_cycle(account_row, label, deposits, withdrawals, fills, balance, client, charge)
        except Exception as exc:  # noqa: BLE001 - a dead cycle is a failed poll, not a silent one
            # Deliberately handled here rather than left to the worker's blanket
            # handler: that one logs "task failed" and moves on, which is how
            # this stayed invisible. Routing it through the normal failure path
            # increments the counter and lets the heartbeat detector do its job.
            logger.error("fast loop for %s failed after the fetches: %s", label, exc)
            self._handle_generic_failure(account_id, label, state, exc)
            return

        _update_poll_state(
            self._db,
            account_id,
            consecutive_auth_failures=0,
            consecutive_failures=0,
            last_success_at=_now_iso(),
            last_fast_loop_at=_now_iso(),
        )
        if self._metrics is not None:
            self._metrics.poll_ok()

    def _run_fast_cycle(self, account_row, label, deposits, withdrawals, fills, balance, client, charge) -> None:
        """Everything between the fetches and recording success.

        Split out so the caller can treat "this raised" as a failed poll. The
        contents are unchanged; only their relationship to `poll_state` is."""
        account_id = account_row["id"]
        state = _get_poll_state(self._db, account_id)
        last_run_iso = state.get("last_fast_loop_at")

        # --- Withdrawals: creation, unseen address, burst -------------------
        # Checked BEFORE record_seen_withdrawal, because that call is what
        # swallows the duplicate - by the time it has returned False the record
        # is gone and there is nothing left to notice (#67).
        self._report_unidentifiable(account_id, label, account_row, withdrawals)

        new_withdrawals = [w for w in withdrawals if record_seen_withdrawal(self._db, account_id, w)]

        for detection in detect_new_withdrawals(new_withdrawals):
            self._emit(account_id, label, detection)

        with self._db.cursor() as cur:
            cur.execute("SELECT address FROM guardian_known_addresses WHERE account_id = %s", (account_id,))
            known = {r["address"] for r in cur.fetchall()}

        for detection in detect_unseen_address(new_withdrawals, known):
            self._emit(account_id, label, detection)

        if new_withdrawals:
            window_minutes = self._rules.get_int("detectors", "withdrawal_burst", "window_minutes", default=60)
            min_count = self._rules.get_int("detectors", "withdrawal_burst", "min_count", default=3)
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT first_seen_at FROM guardian_seen_withdrawals WHERE account_id = %s",
                    (account_id,),
                )
                times = [datetime.fromisoformat(r["first_seen_at"]) for r in cur.fetchall()]
            for detection in detect_withdrawal_burst(times, window_minutes, min_count, _now()):
                self._emit(account_id, label, detection)

        # Record this cycle's addresses into history only after judging them
        # against prior history, so a withdrawal is never compared to itself.
        new_deposit_addresses = []
        for record in deposits:
            if record_known_address(self._db, account_id, record.get("address"), record.get("currency"), "deposit_history"):
                new_deposit_addresses.append(record.get("address"))
        for w in new_withdrawals:
            record_known_address(self._db, account_id, w.get("address"), w.get("currency"), "withdrawal_history")

        for detection in detect_baseline_drift(new_deposit_addresses):
            self._emit(account_id, label, detection)

        # --- Trading: new pairs, off-market fills, fee anomaly --------------
        known_pairs = known_traded_pairs(self._db, account_id)
        for detection in detect_new_pair_activity(fills, known_pairs):
            self._emit(account_id, label, detection)
        for fill in fills:
            record_traded_pair(self._db, account_id, fill.get("symbol"))

        # Which fills get judged for off-market execution and fee spend.
        #
        # Two rules, and they answer different questions (G-47):
        #
        #   *Have I judged this one already?* - by identity, via seen_fills.
        #     The old code answered this with the timestamp alone, which read a
        #     missing one as 0 and so dropped undated fills out of the window
        #     forever. They were never price-checked, on this cycle or any
        #     later one, while still reaching the new-pair detector and the
        #     traded-pair baseline. It looked watched.
        #
        #   *Is the reference price valid evidence about it?* - by time. The
        #     off-market check compares a fill against the ticker as it is
        #     right now, so a fill known to predate the last poll must stay
        #     out: it would be measured against a price that moved since, and
        #     the deviation would be the market's, not an attacker's.
        #
        # An undated fill can only be answered by the first rule. Judging it on
        # first sighting presumes it is recent, which is what its appearance in
        # a fresh response is evidence for - the same presumption
        # record_seen_withdrawal already makes for an undated withdrawal.
        last_run_ms = None
        if last_run_iso:
            # Floored to whole milliseconds, because exchange timestamps are
            # integer ms. Comparing an int against a float carrying sub-ms
            # precision drops a fill stamped inside the boundary millisecond -
            # a one-millisecond hole, but the same silently-never-examined
            # shape as the bug this filter was rewritten to fix (G-47). Erring
            # towards examining is the safe direction here.
            last_run_ms = int(datetime.fromisoformat(last_run_iso).timestamp() * 1000)

        recent_fills = []
        for fill in fills:
            dated_ms = _fill_timestamp_ms(fill, label)
            if last_run_ms is not None and dated_ms is not None and dated_ms < last_run_ms:
                continue  # dated, and older than this window
            # Undated fills are already counted on
            # sentinel_untimestamped_records_total{source="fill"} by the
            # quantity-drop path below, which walks the same list on the same
            # pass. Counting them again here would double the series and put a
            # phantom trend under whatever alarm binds to it.
            if record_seen_fill(self._db, account_id, fill):
                recent_fills.append(fill)

        if recent_fills:
            symbols = sorted({f.get("symbol") for f in recent_fills if f.get("symbol")})
            reference_prices: dict[str, Decimal] = {}
            if symbols:
                tickers: dict = {}
                try:
                    self._acquire(account_id, 1)
                    tickers = client.fetch_tickers(symbols)
                except Exception as exc:  # noqa: BLE001 - public data failure must not kill the loop
                    # Bybit rejects a batch that mixes spot and derivatives
                    # symbols; fall back per-symbol so off-market detection
                    # still runs instead of silently skipping the cycle.
                    logger.warning("batch ticker fetch failed for %s (%s) - falling back to per-symbol", label, exc)
                    for sym in symbols[:10]:
                        try:
                            self._acquire(account_id, 1)
                            tickers[sym] = client.fetch_ticker(sym)
                        except Exception as sym_exc:  # noqa: BLE001
                            logger.warning("ticker fetch failed for %s/%s: %s", label, sym, sym_exc)
                for sym, ticker in tickers.items():
                    last = to_decimal(ticker.get("last") or ticker.get("close"))
                    if last > 0:
                        reference_prices[sym] = last

            max_dev = self._rules.get_decimal("detectors", "off_market", "max_deviation_pct", default=Decimal("10"))
            for detection in detect_off_market_fills(recent_fills, reference_prices, max_dev):
                self._emit(account_id, label, detection)

            window_fees = sum_fees_by_coin(recent_fills)
            ema = load_fee_baseline(self._db, account_id)
            multiplier = self._rules.get_decimal("detectors", "fee_anomaly", "multiplier", default=Decimal("5"))
            floor = self._rules.get_decimal("detectors", "fee_anomaly", "min_fee_floor", default=Decimal("1"))
            alpha = self._rules.get_decimal("detectors", "fee_anomaly", "ema_alpha", default=Decimal("0.2"))
            for detection in detect_fee_anomaly(window_fees, ema, multiplier, floor):
                self._emit(account_id, label, detection)
            for coin, spent in window_fees.items():
                ema[coin] = update_ema(ema.get(coin), spent, alpha)
            if window_fees:
                save_fee_baseline(self._db, account_id, ema)

        # --- Transfers: unfamiliar routes -----------------------------------
        totals = balance.get("total", {}) if isinstance(balance, dict) else {}
        current_balances = {k: v for k, v in totals.items() if v}

        max_coins = self._rules.get_int("detectors", "transfers", "max_coins_per_cycle", default=5)
        top_coins = sorted(current_balances, key=lambda c: to_decimal(current_balances[c]), reverse=True)[:max_coins]
        new_transfers = []
        for coin in top_coins:
            try:
                self._acquire(account_id, 1)
                for transfer in client.fetch_transfers(coin, acquire=charge):
                    if record_seen_transfer(self._db, account_id, transfer):
                        new_transfers.append(transfer)
            except Exception as exc:  # noqa: BLE001 - per-coin; a bad coin must not kill the loop
                logger.warning("transfer fetch failed for %s/%s: %s", label, coin, exc)

        if new_transfers:
            routes = known_transfer_routes(self._db, account_id)
            for detection in detect_new_transfer_route(new_transfers, routes):
                self._emit(account_id, label, detection)
            for transfer in new_transfers:
                route = f"{transfer.get('fromAccount') or '?'}->{transfer.get('toAccount') or '?'}"
                record_transfer_route(self._db, account_id, route)

        # --- Balance: unexplained quantity decrease --------------------------
        previous_balances, snapshot_at = latest_balance_snapshot_at(self._db, account_id)
        current_decimal = {k: to_decimal(v) for k, v in current_balances.items()}

        tolerance_pct = self._rules.get_decimal("detectors", "quantity_drop", "tolerance_pct")
        tolerance_abs = self._rules.get_decimal("detectors", "quantity_drop", "tolerance_absolute")

        # The drop is measured from the previous balance snapshot, so only
        # activity at or after that snapshot can explain it. `withdrawals` and
        # `fills` are fetched with since=None and carry the exchange's full
        # default history - days of it - so they must be windowed here. Without
        # this, a legitimate sale from last week cancels out a theft from the
        # last 45 seconds and nothing is ever alerted (G-7).
        window_start_ms = None
        if snapshot_at is not None:
            grace = self._rules.get_decimal(
                "detectors", "quantity_drop", "explanation_grace_seconds", default=Decimal("60")
            )
            window_start = datetime.fromisoformat(snapshot_at) - timedelta(seconds=float(grace))
            window_start_ms = int(window_start.timestamp() * 1000)

        for detection in detect_quantity_drop(
            previous_balances,
            current_decimal,
            withdrawals,
            fills,
            tolerance_pct,
            tolerance_abs,
            window_start_ms=window_start_ms,
            # The metric dimension is the exchange; the log line names the
            # account. Passing `label` for both keyed the undated-record
            # counter by customer, so it named who noticed rather than which
            # integration changed, and grew a series per account (#55).
            exchange=_account_exchange(account_row),
            account_label=label,
            metrics=self._metrics,
        ):
            self._emit(account_id, label, detection)

        record_balance_snapshot(self._db, account_id, current_balances)

    def run_slow_loop(self, account_row) -> None:
        account_id = account_row["id"]
        label = account_row["label"]
        try:
            client = _load_client(self._cipher, account_row)
            self._acquire(account_id, 1)
            info = client.fetch_account_info()
        except TamperSignal as exc:
            state = _get_poll_state(self._db, account_id)
            self._handle_auth_failure(account_id, label, state, exc)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("slow loop failed for %s: %s", label, exc)
            return

        payload = info.get("data", info) if isinstance(info, dict) else info
        current = {
            "authorities": payload.get("authorities") if isinstance(payload, dict) else None,
            "ip_whitelist": payload.get("ips") if isinstance(payload, dict) else None,
        }
        previous = latest_permission_snapshot(self._db, account_id)
        for detection in detect_permission_state_change(previous, current):
            self._emit(account_id, label, detection)

        record_permission_snapshot(self._db, account_id, current["authorities"], current["ip_whitelist"])
        # Every ten minutes, this payload names the exchange account the
        # working key belongs to. Recording it is what lets a customer replace
        # a revoked key themselves later (#69) - and doing it here rather than
        # only at onboarding is what gives every account already in the fleet
        # an identity within one slow loop, instead of never.
        outcome = learn_exchange_uid(
            self._db, account_id, exchange_account_uid(account_row["exchange"], payload))
        if outcome == "mismatch":
            # Not made into a detection: the same key answering with a
            # different account id is far more likely to be an exchange
            # changing its response shape than an attack, and a new CRITICAL
            # rule that fires on a vendor's JSON edit is how alert fatigue
            # starts. Logged so a person can see it, and the stored id is left
            # alone so the replacement check keeps meaning something.
            logger.warning("account %s: exchange reports a different account id than the one on "
                           "record - key replacement will refuse until this is looked at", label)
        # Housekeeping on the slow cadence: balance snapshots older than the
        # retention window only bloat the table the fast loop queries.
        prune_balance_snapshots(self._db, account_id)
        _update_poll_state(self._db, account_id, last_slow_loop_at=_now_iso())

    def _handle_auth_failure(self, account_id: int, label: str, state: dict, exc: Exception) -> None:
        failures = state.get("consecutive_auth_failures", 0) + 1
        _update_poll_state(self._db, account_id, consecutive_auth_failures=failures, consecutive_failures=0)
        threshold = self._rules.get_int("detectors", "tamper", "consecutive_auth_failures_threshold", default=2)
        for detection in detect_tamper(failures, threshold):
            self._emit(account_id, label, detection)

        # An account whose key is rejected is just as unwatched as one whose
        # exchange is down, and G-6's case - an expired or deleted key - arrives
        # here rather than through the generic path. Tamper answers "is this an
        # attack"; this answers "are you covered", and the customer needs the
        # second one to know their key must be replaced. Both fire: they say
        # different things and neither replaces the other.
        unreachable_threshold = self._rules.get_int(
            "detectors", "heartbeat", "consecutive_failures_threshold", default=5
        )
        for detection in detect_unreachable(failures, unreachable_threshold, auth_failures=failures):
            self._emit(account_id, label, detection)

        if self._metrics is not None:
            self._metrics.poll_failed(auth=True)
        logger.warning("auth/permission failure for %s (%d consecutive): %s", label, failures, exc)

    def _handle_generic_failure(self, account_id: int, label: str, state: dict, exc: Exception) -> None:
        failures = state.get("consecutive_failures", 0) + 1
        _update_poll_state(self._db, account_id, consecutive_failures=failures)
        threshold = self._rules.get_int("detectors", "heartbeat", "consecutive_failures_threshold", default=5)
        auth_failures = state.get("consecutive_auth_failures", 0)
        for detection in detect_unreachable(failures, threshold, auth_failures=auth_failures):
            self._emit(account_id, label, detection)
        if self._metrics is not None:
            self._metrics.poll_failed(auth=False)
        logger.warning("poll failure for %s (%d consecutive): %s", label, failures, exc)


def run_forever(db: Database, cipher: CredentialCipher, rules: RulesConfig, channels: dict) -> None:
    """Sequential single-process loop, kept for small deployments and
    debugging. The scaled path is sentinel/engine.py (worker pool +
    rate limiting + circuit breakers)."""
    poller = AccountPoller(db, cipher, rules, channels)
    last_slow_loop: dict[int, float] = {}

    while True:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM guardian_accounts WHERE status = 'active'")
            accounts = cur.fetchall()

        for account_row in accounts:
            poller.run_fast_loop(account_row)
            account_id = account_row["id"]
            slow_interval = rules.get_seconds("polling", "slow_loop_seconds", default=600)
            now = time.monotonic()
            # Same "never polled is None, not 0" as Engine._schedule_cycle -
            # monotonic() counts from boot, so comparing against 0 asked about
            # the machine's uptime rather than this account's last slow poll.
            last_slow = last_slow_loop.get(account_id)
            if last_slow is None or now - last_slow >= slow_interval:
                poller.run_slow_loop(account_row)
                last_slow_loop[account_id] = now

        jitter = rules.get_seconds("polling", "jitter_seconds")
        fast_interval = rules.get_seconds("polling", "fast_loop_seconds", default=45)
        time.sleep(fast_interval + random.uniform(0, jitter))
