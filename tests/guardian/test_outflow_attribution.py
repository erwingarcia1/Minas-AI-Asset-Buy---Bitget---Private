"""Which movements count as explaining a balance drop.

Every test here kills a mutant that survived `scripts/mutation-check.sh`. They
are grouped by what the mutation would do if it were real, not by which function
it lives in — because the consequences are wildly different and only one of them
is "an alert fires that shouldn't".

The dangerous direction is **over-crediting**: if the wrong movement counts as
an explanation, a real theft looks accounted for and no alert fires at all. Half
of `_explained_outflow_for_coin`'s conditions are `and`s whose mutation to `or`
credits outflow that never happened, and not one of them was pinned.
"""

from __future__ import annotations

from decimal import Decimal

from guardian.detectors.fee_anomaly import sum_fees_by_coin
from guardian.detectors.quantity_drop import _explained_outflow_for_coin

TOL = Decimal("0.5")


def _fill(symbol="BTC/USDT", side="sell", filled="1", cost="50000", fee=None):
    record = {"symbol": symbol, "side": side, "filled": filled, "cost": cost}
    if fee is not None:
        record["fee"] = fee
    return record


def _withdrawal(currency="BTC", amount="1", fee=None):
    record = {"currency": currency, "amount": amount}
    if fee is not None:
        record["fee"] = fee
    return record


# --- over-crediting: a theft that looks explained ----------------------------

def test_a_sell_of_a_different_coin_does_not_explain_this_coins_drop():
    """`side == "sell" and base == coin` → `or` survived. Under the mutation,
    *any* sell explains *any* coin's drop: 1 BTC vanishes, an unrelated ETH sale
    accounts for it, and nothing alerts."""
    outflow = _explained_outflow_for_coin("BTC", [], [_fill(symbol="ETH/USDT", side="sell")])
    assert outflow == Decimal("0")


def test_a_buy_priced_in_a_different_coin_does_not_explain_this_coins_drop():
    """`side == "buy" and quote == coin` → `or` survived. Under the mutation a
    buy settled in USDT explains a drop in BTC."""
    outflow = _explained_outflow_for_coin("BTC", [], [_fill(symbol="ETH/USDT", side="buy", cost="900")])
    assert outflow == Decimal("0")


def test_a_sell_settled_in_this_coin_does_not_count_as_a_buy():
    """`side == "buy"` → `side != "buy"` survived: a *sell* of BTC/USDT would
    then also credit its USDT cost against USDT, explaining a USDT drop that
    the sale in fact caused an *increase* in."""
    outflow = _explained_outflow_for_coin("USDT", [], [_fill(symbol="BTC/USDT", side="sell", cost="50000")])
    assert outflow == Decimal("0")


def test_a_buy_credits_only_the_coin_it_was_paid_in():
    """`quote == coin` → `!=` survived. Both directions are asserted: the quote
    coin is credited, and nothing else is."""
    fill = _fill(symbol="BTC/USDT", side="buy", cost="50000")
    assert _explained_outflow_for_coin("USDT", [], [fill]) == Decimal("50000")
    assert _explained_outflow_for_coin("EUR", [], [fill]) == Decimal("0")


def test_a_sell_credits_the_base_coin_it_actually_sold():
    fill = _fill(symbol="BTC/USDT", side="sell", filled="2")
    assert _explained_outflow_for_coin("BTC", [], [fill]) == Decimal("2")


def test_a_fee_in_another_coin_does_not_explain_this_coins_drop():
    """`fee.get("currency") == coin` → `!=` survived, in both loops. Under the
    mutation every fee is credited to every coin except the one that paid it."""
    withdrawal = _withdrawal(currency="BTC", amount="1", fee={"currency": "USDT", "cost": "5"})
    assert _explained_outflow_for_coin("BTC", [withdrawal], []) == Decimal("1")

    fill = _fill(symbol="BTC/USDT", side="sell", filled="1", fee={"currency": "USDT", "cost": "5"})
    assert _explained_outflow_for_coin("BTC", [], [fill]) == Decimal("1")


def test_a_withdrawal_of_another_coin_does_not_stop_the_scan():
    """`continue` → `break` survived in the withdrawals loop. One withdrawal of
    an unrelated coin would end the scan, so every withdrawal after it stops
    explaining anything - and the drop it did explain alerts as unexplained."""
    withdrawals = [_withdrawal(currency="ETH", amount="9"), _withdrawal(currency="BTC", amount="3")]
    assert _explained_outflow_for_coin("BTC", withdrawals, []) == Decimal("3")


# --- under-crediting: a false alarm ------------------------------------------

def test_a_withdrawal_fee_in_the_same_coin_is_credited():
    """`w.get("fee") or {}` → `and {}` survived. The mutation makes `fee`
    empty whenever a fee exists, so the fee leaves the account but is never
    counted as having left - and the shortfall alerts as unexplained."""
    withdrawal = _withdrawal(currency="BTC", amount="1", fee={"currency": "BTC", "cost": "0.0005"})
    assert _explained_outflow_for_coin("BTC", [withdrawal], []) == Decimal("1.0005")


def test_a_fill_fee_in_the_same_coin_is_credited():
    """Same mutation, the fills loop."""
    fill = _fill(symbol="BTC/USDT", side="sell", filled="1", fee={"currency": "BTC", "cost": "0.001"})
    assert _explained_outflow_for_coin("BTC", [], [fill]) == Decimal("1.001")


# --- fee summing --------------------------------------------------------------

def test_a_fee_with_no_currency_is_not_summed_under_a_null_coin():
    """`coin and cost > 0` → `coin or cost > 0` survived. The mutation sums a
    currency-less fee under the key `None`, which then reaches the fee-anomaly
    EMA and the alert evidence as a coin named "None"."""
    fees = sum_fees_by_coin([_fill(fee={"cost": "5"})])
    assert None not in fees
    assert fees == {}


def test_a_zero_fee_is_not_recorded():
    """`cost > 0` → `>= 0` survived: a zero-cost fee would create a coin entry
    of 0, seeding the EMA baseline with a value no trade produced."""
    assert sum_fees_by_coin([_fill(fee={"currency": "USDT", "cost": "0"})]) == {}


def test_real_fees_are_summed_per_coin():
    fees = sum_fees_by_coin([
        _fill(fee={"currency": "USDT", "cost": "3"}),
        _fill(fee={"currency": "USDT", "cost": "4"}),
        _fill(fee={"currency": "BNB", "cost": "1"}),
    ])
    assert fees == {"USDT": Decimal("7"), "BNB": Decimal("1")}


# --- false alarms: a zero that shouldn't pass a guard ------------------------

def test_a_fill_with_a_zero_price_is_skipped_not_reported_as_100_percent_off():
    """`price <= 0` → `price < 0` survived in `detect_off_market_fills`.

    Under the mutation a fill priced at zero passes the guard, and
    `abs(0 - reference) / reference * 100` is exactly 100% — a HIGH
    "possible counterparty-drain trade" manufactured out of a missing field.
    False alarms teach a customer to mute, and a muted customer is an
    unprotected one."""
    from guardian.detectors.off_market import detect_off_market_fills

    fills = [{"symbol": "XYZ/USDT", "average": "0", "price": "0", "side": "sell", "amount": "1", "id": "z"}]
    assert detect_off_market_fills(fills, {"XYZ/USDT": Decimal("1.0")}, Decimal("10")) == []


def test_a_reference_price_of_zero_is_skipped_not_divided_by():
    """`reference <= 0` → `< 0` survived. Under the mutation a reference of
    exactly zero reaches `abs(price - reference) / reference` — a
    ZeroDivisionError out of the middle of the fill loop, which since #59 is a
    failed poll for that whole account."""
    from guardian.detectors.off_market import detect_off_market_fills

    fills = [{"symbol": "XYZ/USDT", "average": "5", "side": "sell", "amount": "1", "id": "z"}]
    assert detect_off_market_fills(fills, {"XYZ/USDT": Decimal("0")}, Decimal("10")) == []
