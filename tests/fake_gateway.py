"""Configurable fake exchange gateway for executor tests (new idempotent interface).

Default behaviour: every buy fills at 50,000 USDT/BTC with a 0.1% taker fee
in the base coin; autobuy withdrawals succeed with a fixed fee. Knobs:

- ``fail_buy``: place_buy raises ExecutionError (order never sent)
- ``place_raises_after_sending``: place_buy raises but the order DID reach the
  exchange (lookup then finds it filled)
- ``fill_unavailable``: fetch_fill raises (order placed, fill unknown)
- ``lookup_raises`` / ``lookup_state``: control reconciliation answers
- ``fail_withdrawal`` / ``withdrawal_needs_info``: withdrawal outcomes
- ``withdrawal_state``: what withdrawal_status reports (None = not found,
  "pending", "success", "wallet_fail", ...); ``status_raises``: lookup fails
"""
from __future__ import annotations

from api.tasks import purchase_scheduler as ps


class FakeGateway:
    PRICE = 50_000.0
    FEE_RATE = 0.001

    def __init__(self, *, fail_buy=False, place_raises_after_sending=False, fill_unavailable=False,
                 lookup_raises=False, lookup_state=None, fail_withdrawal=False, withdrawal_needs_info=False,
                 withdrawal_state="pending", status_raises=False):
        self.fail_buy = fail_buy
        self.place_raises_after_sending = place_raises_after_sending
        self.fill_unavailable = fill_unavailable
        self.lookup_raises = lookup_raises
        self.lookup_state = lookup_state
        self.fail_withdrawal = fail_withdrawal
        self.withdrawal_needs_info = withdrawal_needs_info
        self.withdrawal_state = withdrawal_state
        self.status_raises = status_raises
        self.status_calls: list = []
        self.placed: list = []       # (client_oid, purchase_id, dry_run) — orders that reached the "exchange"
        self.lookups: list = []
        self.withdrawals: list = []
        self.n = 0

    # -- helpers ------------------------------------------------------------
    def _fill_for(self, purchase, order_id):
        amount = purchase.amount_usd / self.PRICE
        return {"order_id": order_id, "filled": True, "raw_status": "filled",
                "amount_crypto": amount, "exchange_rate": self.PRICE, "cost_usdt": purchase.amount_usd,
                "fee": amount * self.FEE_RATE, "fee_currency": purchase.crypto_symbol}

    @property
    def calls(self):
        """(account_id, purchase_id, dry_run) per order that reached the exchange (legacy assertion helper)."""
        return [(None, pid, dry) for _, pid, dry in self.placed]

    # -- gateway interface --------------------------------------------------
    def place_buy(self, account, purchase, client_oid, dry_run):
        if self.fail_buy:
            raise ps.ExecutionError("MIN_NOTIONAL: too small", stage="buy", code="MIN_NOTIONAL")
        self.n += 1
        self.placed.append((client_oid, purchase.id, dry_run))
        if self.place_raises_after_sending:
            raise ps.ExecutionError("NETWORK: read timed out after the order was sent", stage="buy", code="NETWORK")
        return {"order_id": f"ORD-{self.n}", "dry_run": dry_run}

    def fetch_fill(self, account, purchase, order_id, client_oid, dry_run):
        if self.fill_unavailable:
            raise ps.ExecutionError("HTTP: order detail unavailable (503)", stage="fill", code="HTTP")
        return self._fill_for(purchase, order_id)

    def lookup_order(self, account, purchase, client_oid):
        self.lookups.append(client_oid)
        if self.lookup_raises:
            raise ps.ExecutionError("NETWORK: exchange unreachable", stage="lookup", code="NETWORK")
        if self.lookup_state is not None:
            state = self.lookup_state
            fill = self._fill_for(purchase, f"ORD-{client_oid[-4:]}") if state == "filled" else None
            return {"state": state, "fill": fill}
        sent = [p for p in self.placed if p[0] == client_oid]
        if not sent:
            return {"state": "not_found", "fill": None}
        idx = self.placed.index(sent[0]) + 1
        return {"state": "filled", "fill": self._fill_for(purchase, f"ORD-{idx}")}

    def withdraw(self, account, purchase, amount, client_oid, dry_run):
        self.withdrawals.append((client_oid, purchase.id, amount, dry_run))
        if self.withdrawal_needs_info:
            raise ps.ExecutionError("40053: travel rule information (beneficiary first name / last name) required",
                                    stage="withdraw", code="40053")
        if self.fail_withdrawal:
            raise ps.ExecutionError("WITHDRAW_UNSUPPORTED: exchange said no", stage="withdraw", code="WITHDRAW_UNSUPPORTED")
        n = len(self.withdrawals)
        return {"withdrawal_id": f"WD-{n}", "chain": purchase.blockchain_network, "withdrawal_fee": 0.0001,
                "withdrawal_fee_currency": purchase.crypto_symbol, "tx_hash": "0xhash" if not dry_run else None,
                "raw_status": "simulated" if dry_run else "submitted"}


    def withdrawal_status(self, account, purchase, wd):
        self.status_calls.append((wd.client_oid, wd.withdrawal_id))
        if self.status_raises:
            raise ps.ExecutionError("NETWORK: exchange unreachable", stage="withdrawal_status", code="NETWORK")
        state = self.withdrawal_state
        if state is None:
            return {"found": False, "raw_status": None, "tx_hash": None, "fee": None, "done": False, "ok": None}
        done = state in ("success", "wallet_fail", "cancel", "reject", "fail", "failed")
        return {"found": True, "raw_status": state, "tx_hash": "0xconfirmed" if state == "success" else None,
                "fee": 0.00003 if state == "success" else None, "done": done, "ok": (state == "success") if done else None}


class FailingGateway(FakeGateway):
    """Withdrawal always fails (the buy fills)."""

    def __init__(self):
        super().__init__(fail_withdrawal=True)


class BuyFails(FakeGateway):
    """The buy itself is rejected (never reaches the exchange)."""

    def __init__(self):
        super().__init__(fail_buy=True)


# RichGateway kept as an alias: the default fake already returns full fill details.
RichGateway = FakeGateway
