"""Customer-facing wording for failed buys (issues #90/#91).

One translation layer: the raw venue string (with its code) stays on the rows,
in admin views, Slack and Telegram; customers get plain English — what
happened, whether money moved (never), and exactly what to do in Bitget.
Owner-approved wording, 2026-10-12.

The Bitget menu labels live in BITGET_LABELS only, so the owner can correct
them against the real app before deploy without touching sentence code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("autobuy.failure_text")

# ---------------------------------------------------------------- Bitget words
# PENDING OWNER CHECK against the live Bitget app (2026-10-12): every menu or
# field label a customer is told to click lives here and nowhere else.
BITGET_LABELS = {
    "transfer_path": "Assets → Transfer",
    "spot_account": "Spot account",
    "unified_account": "Unified Trading account",
    "funding_account": "Funding account",
    "api_management": "API Management",
    "spot_trading_permission": "Spot trading",
    "unified_mgmt_read": "Unified Trading Account → Management → Read",
}

HELP_FOOTER = "Need help? Reply to this email or ask in the AutoBuy Telegram group."

# 1% headroom over the purchase amount, same margin as the pre-buy warning.
TOP_UP_MARGIN = 0.01

# ------------------------------------------------------------- classification

INSUFFICIENT_BALANCE = "insufficient_balance"
BELOW_MINIMUM = "below_minimum"
PAIR_NOT_TRADING = "pair_not_trading"
NO_PERMISSION = "no_permission"
UNKNOWN = "unknown"

# Venue/engine codes per class. Classic twins included for PRESENTATION only —
# execution behavior is the executor's DEFINITE_REFUSAL_CODES, unchanged.
CLASS_CODES = {
    INSUFFICIENT_BALANCE: {"43012", "25202"},
    BELOW_MINIMUM: {"MIN_NOTIONAL", "25207", "25611"},
    PAIR_NOT_TRADING: {"SYMBOL", "25100", "25101", "25102"},
    NO_PERMISSION: {"40014", "25013", "25620"},
}
_CODE_TO_CLASS = {code: cls for cls, codes in CLASS_CODES.items() for code in codes}


def classify(raw: Optional[str]) -> str:
    """Failure class of a stored error string. The raw text may carry executor
    prefixes ("missed this cycle …", "PURCHASE STOPPED …"); any known code
    token anywhere in it decides. Unknown stays unknown — never guessed."""
    if not raw:
        return UNKNOWN
    for token in (t.strip().rstrip(":") for t in raw.replace("(", " ").replace(")", " ").split()):
        if token in _CODE_TO_CLASS:
            return _CODE_TO_CLASS[token]
    return UNKNOWN


# ------------------------------------------------------------- balance context

@dataclass
class BalanceContext:
    """Read-only venue balances at email time; None = could not be read."""
    trading: Optional[float] = None
    funding: Optional[float] = None   # Unified accounts only


def read_balance_context(account) -> BalanceContext:
    """Best-effort synchronous reads (callers use asyncio.to_thread). Any
    failure degrades to None — the wording then simply omits the figures."""
    ctx = BalanceContext()
    if account is None:
        return ctx
    try:
        from api.services.exchange_gateway import _client_for
        client = _client_for(account)
        ctx.trading = float(client.get_account_balance("USDT"))
        if (getattr(account, "account_mode", "") or "") == "unified":
            try:
                ctx.funding = float(client.get_funding_available("USDT"))
            except Exception:   # noqa: BLE001 — hint only, never blocks the email
                logger.info("funding balance unreadable for account %s", account.id)
    except Exception:   # noqa: BLE001
        logger.info("trading balance unreadable for account %s", account.id)
    return ctx


# ---------------------------------------------------------------- the wording

def _fmt(amount: float) -> str:
    from api.services.history import fmt_price
    return fmt_price(amount)


def _mode(account) -> str:
    return (getattr(account, "account_mode", "") or "") if account is not None else ""


def _account_word(account) -> str:
    return (BITGET_LABELS["unified_account"] if _mode(account) == "unified"
            else "Bitget account")


def reason_for(cls: str, purchase, account) -> str:
    """One or two short sentences: what happened. No codes."""
    coin = purchase.crypto_symbol
    if cls == INSUFFICIENT_BALANCE:
        return f"Your {_account_word(account)} didn't have enough USDT."
    if cls == BELOW_MINIMUM:
        minimum = _bitget_minimum(coin)
        if minimum:
            return (f"Bitget needs at least {_fmt(minimum)} USDT per {coin} order, "
                    f"and this purchase is set to {_fmt(purchase.amount_usd)} USDT.")
        return f"This purchase is below Bitget's minimum order size for {coin}."
    if cls == PAIR_NOT_TRADING:
        return (f"Bitget has paused trading for {coin}/USDT right now, "
                "so this buy couldn't go through.")
    if cls == NO_PERMISSION:
        return "Bitget refused the buy because your API key doesn't allow trading."
    return "Bitget couldn't accept this buy. We checked with Bitget and made sure no order went through."


def balance_lines(purchase, ctx: Optional[BalanceContext]) -> str:
    """'You have X USDT. Add about Y USDT more.' — only when the trading
    balance could be read; covers-it wording when it already does."""
    if ctx is None or ctx.trading is None:
        return ""
    needed = float(purchase.amount_usd) * (1.0 + TOP_UP_MARGIN)
    missing = needed - ctx.trading
    if missing <= 0:
        return (f"You have {_fmt(ctx.trading)} USDT — that now covers this buy, "
                "so the next attempt should go through. ")
    return f"You have {_fmt(ctx.trading)} USDT. Add about {_fmt(missing)} USDT more. "


def action_for(cls: str, purchase, account, ctx: Optional[BalanceContext] = None) -> str:
    """Exactly what to do next, in Bitget words."""
    L = BITGET_LABELS
    coin = purchase.crypto_symbol
    unified = _mode(account) == "unified"
    if cls == INSUFFICIENT_BALANCE:
        funding_hint = _funding_hint(purchase, ctx) if unified else ""
        if funding_hint:
            return funding_hint
        where = L["unified_account"] if unified else L["spot_account"]
        extra = (f"Money in the {L['funding_account']} can't be used for the buy."
                 if unified else "Money in Funding, Earn or Futures can't be used for the buy.")
        return (f"To top up: in Bitget, go to {L['transfer_path']} and move USDT into the "
                f"{where} of the sub-account AutoBuy uses. {extra}")
    if cls == BELOW_MINIMUM:
        minimum = _bitget_minimum(coin)
        target = f" to at least {_fmt(minimum)} USDT" if minimum else ""
        return (f"To fix it: open the purchase in AutoBuy and raise the amount{target} — "
                "it will run normally from the next attempt.")
    if cls == PAIR_NOT_TRADING:
        return ("This is usually temporary and we keep trying on your schedule. "
                "If it lasts more than a day, consider a different coin or contact us.")
    if cls == NO_PERMISSION:
        if unified:
            return (f"To fix it: in Bitget, open {L['api_management']} and edit the key AutoBuy uses — "
                    f"allow trading and tick {L['unified_mgmt_read']}. "
                    "Then open AutoBuy Settings → Exchange keys and press Verify.")
        return (f"To fix it: in Bitget, open {L['api_management']} and edit the key AutoBuy uses — "
                f"allow {L['spot_trading_permission']}. "
                "Then open AutoBuy Settings → Exchange keys and press Verify.")
    return ("One-off hiccups usually clear on their own. "
            "Our team is alerted automatically when they don't.")


def _funding_hint(purchase, ctx: Optional[BalanceContext]) -> str:
    """#91: when USDT sits in Funding, say so — 'enough to cover this' only
    when Funding covers the shortfall (owner rule 4)."""
    if ctx is None or ctx.funding is None or ctx.funding <= 0 or ctx.trading is None:
        return ""
    L = BITGET_LABELS
    needed = float(purchase.amount_usd) * (1.0 + TOP_UP_MARGIN)
    shortfall = max(needed - ctx.trading, 0.0)
    if shortfall <= 0:
        return ""
    move = (f"In Bitget, go to {L['transfer_path']} for the sub-account AutoBuy uses, "
            f"choose From: {L['funding_account']} and To: {L['unified_account']}, "
            "and move it across.")
    if ctx.funding >= shortfall:
        return (f"Good news: you have {_fmt(ctx.funding)} USDT in your Bitget "
                f"{L['funding_account']} — enough to cover this. {move} "
                "That's all you need to do.")
    return (f"You have {_fmt(ctx.funding)} USDT in your Bitget {L['funding_account']}. "
            f"{move} Then add about {_fmt(shortfall - ctx.funding)} USDT more.")


def _bitget_minimum(coin: str) -> Optional[float]:
    """The venue minimum from the hour-cached public symbol data; None off-cache."""
    try:
        from api.services import bitget_market
        symbol = bitget_market.get_symbol_sync(coin)
        return float(symbol["min_trade_usdt"]) if symbol and symbol.get("min_trade_usdt") else None
    except Exception:   # noqa: BLE001 — wording survives a market-data hiccup
        return None


def in_app_line(cls: str, coin: str) -> str:
    """Short static line for the purchase row and the notification feed.
    No live figures: the purchase list renders without venue calls."""
    if cls == INSUFFICIENT_BALANCE:
        return "Not enough USDT in your Bitget account. Top up and the next attempt runs normally."
    if cls == BELOW_MINIMUM:
        return f"Below Bitget's minimum order size for {coin}. Edit the purchase and raise the amount."
    if cls == PAIR_NOT_TRADING:
        return f"{coin}/USDT is paused on Bitget right now. Nothing was bought; we keep trying on schedule."
    if cls == NO_PERMISSION:
        return "Your Bitget API key doesn't allow trading. Fix it in Bitget, then press Verify in Settings."
    return "Bitget couldn't accept this buy. Nothing was bought; we'll retry at the next attempt."


def customer_error_line(raw: Optional[str], coin: str) -> Optional[str]:
    """What the app shows for a stored buy error: the plain line for a known
    class; an UNKNOWN raw string passes through unchanged (it may describe a
    withdrawal or something new — never relabel what we can't classify)."""
    if not raw:
        return raw
    cls = classify(raw)
    if cls == UNKNOWN:
        return raw
    return in_app_line(cls, coin)
