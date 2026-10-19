"""Customer-facing failure wording (#90/#91): plain English, no venue codes,
money-never-moved stated, Bitget steps per account mode, figures when the
balance could be read, Funding hint rules, and the feed cleanup."""
import re
from types import SimpleNamespace

import pytest

from api.services import failure_text as ft

pytestmark = pytest.mark.asyncio


def purchase(coin="PEPE", amount=2.0):
    return SimpleNamespace(crypto_symbol=coin, amount_usd=amount)


def account(mode="unified"):
    return SimpleNamespace(account_mode=mode, id="k")


CLASSIC, UNIFIED = account("classic"), account("unified")


# ------------------------------------------------------------- classification

def test_classification_from_raw_strings():
    assert ft.classify("25202: Insufficient balance") == ft.INSUFFICIENT_BALANCE
    assert ft.classify("43012: Insufficient balance") == ft.INSUFFICIENT_BALANCE
    assert ft.classify("PURCHASE STOPPED after 3 failed attempts: 43012: Insufficient balance") == ft.INSUFFICIENT_BALANCE
    assert ft.classify("missed this cycle (1 of 3 in a row) after 3 attempts; next attempt X: "
                       "MIN_NOTIONAL: 0.5 USDT is below the 1 USDT minimum") == ft.BELOW_MINIMUM
    for code in ("25207", "25611"):
        assert ft.classify(f"{code}: too small") == ft.BELOW_MINIMUM
    for code in ("SYMBOL", "25100", "25101", "25102"):
        assert ft.classify(f"{code}: pair closed") == ft.PAIR_NOT_TRADING
    for code in ("40014", "25013", "25620"):
        assert ft.classify(f"{code}: no") == ft.NO_PERMISSION
    assert ft.classify("sending 1.2 XRP to rXXX failed: travel rule information required") == ft.UNKNOWN
    assert ft.classify(None) == ft.UNKNOWN


def no_codes(text):
    """No venue code may leak into customer text."""
    assert not re.search(r"\b(25\d{3}|43\d{3}|40\d{3}|MIN_NOTIONAL|SYMBOL)\b", text), text


# ------------------------------------------------------------- per-case wording

def test_insufficient_balance_classic_and_unified():
    classic = ft.reason_for(ft.INSUFFICIENT_BALANCE, purchase("BTC", 50), CLASSIC)
    unified = ft.reason_for(ft.INSUFFICIENT_BALANCE, purchase("BTC", 50), UNIFIED)
    assert classic == "Your Bitget account didn't have enough USDT."
    assert unified == "Your Unified Trading account didn't have enough USDT."
    a_classic = ft.action_for(ft.INSUFFICIENT_BALANCE, purchase("BTC", 50), CLASSIC)
    assert "Assets → Transfer" in a_classic and "Spot account" in a_classic
    assert "sub-account AutoBuy uses" in a_classic
    a_unified = ft.action_for(ft.INSUFFICIENT_BALANCE, purchase("BTC", 50), UNIFIED)
    assert "Unified Trading account" in a_unified and "sub-account AutoBuy uses" in a_unified
    assert "Funding account can't be used" in a_unified
    for t in (classic, unified, a_classic, a_unified):
        no_codes(t)


def test_balance_lines_add_more_and_covers():
    p = purchase("BTC", 50)   # needed = 50.50
    assert ft.balance_lines(p, ft.BalanceContext(trading=12.30)) == \
        "You have 12.30 USDT. Add about 38.20 USDT more. "
    covers = ft.balance_lines(p, ft.BalanceContext(trading=60.0))
    assert "that now covers this buy" in covers
    assert ft.balance_lines(p, None) == "" and ft.balance_lines(p, ft.BalanceContext()) == ""


def test_funding_hint_rules():
    p = purchase("PEPE", 50)   # needed 50.50
    # Funding covers the shortfall -> "enough to cover this" (owner rule 4)
    full = ft.action_for(ft.INSUFFICIENT_BALANCE, p, UNIFIED,
                         ft.BalanceContext(trading=12.30, funding=40.0))
    assert "40.00 USDT in your Bitget Funding account — enough to cover this" in full
    assert "From: Funding account" in full and "To: Unified Trading account" in full
    assert "That's all you need to do." in full
    # Funding short -> move it, then add the difference (38.20 - 10 = 28.20)
    part = ft.action_for(ft.INSUFFICIENT_BALANCE, p, UNIFIED,
                         ft.BalanceContext(trading=12.30, funding=10.0))
    assert "You have 10.00 USDT in your Bitget Funding account" in part
    assert "Then add about 28.20 USDT more." in part
    assert "enough to cover this" not in part
    # no funding / zero / classic -> plain instructions, no hint
    for ctx in (None, ft.BalanceContext(trading=12.30, funding=0.0),
                ft.BalanceContext(trading=12.30, funding=None)):
        assert "Funding account —" not in ft.action_for(ft.INSUFFICIENT_BALANCE, p, UNIFIED, ctx)
    assert "Funding account —" not in ft.action_for(
        ft.INSUFFICIENT_BALANCE, p, CLASSIC, ft.BalanceContext(trading=12.30, funding=40.0))


def test_below_minimum_uses_the_cached_venue_minimum():
    reason = ft.reason_for(ft.BELOW_MINIMUM, purchase("PEPE", 0.5), UNIFIED)
    assert reason == ("Bitget needs at least 1.00 USDT per PEPE order, "
                      "and this purchase is set to 0.50 USDT.")
    action = ft.action_for(ft.BELOW_MINIMUM, purchase("PEPE", 0.5), UNIFIED)
    assert "raise the amount to at least 1.00 USDT" in action
    no_codes(reason); no_codes(action)


def test_pair_not_trading():
    reason = ft.reason_for(ft.PAIR_NOT_TRADING, purchase("PEPE"), CLASSIC)
    assert reason == ("Bitget has paused trading for PEPE/USDT right now, "
                      "so this buy couldn't go through.")
    assert "usually temporary" in ft.action_for(ft.PAIR_NOT_TRADING, purchase("PEPE"), CLASSIC)


def test_permission_steps_are_mode_specific_never_both():
    classic = ft.action_for(ft.NO_PERMISSION, purchase("BTC"), CLASSIC)
    unified = ft.action_for(ft.NO_PERMISSION, purchase("BTC"), UNIFIED)
    assert "Spot trading" in classic and "Management → Read" not in classic
    assert "Management → Read" in unified and "Spot trading" not in unified
    for t in (classic, unified):
        assert "API Management" in t and "press Verify" in t
        no_codes(t)


def test_unknown_fallback_is_frame_neutral():
    reason = ft.reason_for(ft.UNKNOWN, purchase("BTC"), CLASSIC)
    assert "made sure no order went through" in reason
    assert "alerted automatically" in ft.action_for(ft.UNKNOWN, purchase("BTC"), CLASSIC)


# ------------------------------------------------------------- in-app lines

def test_in_app_lines_are_short_plain_and_codeless():
    for cls in (ft.INSUFFICIENT_BALANCE, ft.BELOW_MINIMUM, ft.PAIR_NOT_TRADING,
                ft.NO_PERMISSION, ft.UNKNOWN):
        line = ft.in_app_line(cls, "PEPE")
        assert len(line) <= 120
        no_codes(line)


def test_customer_error_line_translates_known_and_passes_unknown_through():
    raw = "PURCHASE STOPPED after 3 failed attempts: 25202: Insufficient balance"
    assert ft.customer_error_line(raw, "PEPE") == ft.in_app_line(ft.INSUFFICIENT_BALANCE, "PEPE")
    withdrawal = "sending 1.2 XRP failed: address not in whitelist"
    assert ft.customer_error_line(withdrawal, "XRP") == withdrawal, \
        "unclassified errors are never relabelled as buy failures"
    assert ft.customer_error_line(None, "XRP") is None


def test_menu_labels_live_in_the_constants_block_only():
    """Owner rule 10: changing a Bitget label must be a one-line edit."""
    import inspect
    source = inspect.getsource(ft)
    after_dict = source.split("BITGET_LABELS = {", 1)[1].split("\n}", 1)[1]
    for label in ft.BITGET_LABELS.values():
        assert label not in after_dict, f"{label!r} hardcoded outside BITGET_LABELS"
