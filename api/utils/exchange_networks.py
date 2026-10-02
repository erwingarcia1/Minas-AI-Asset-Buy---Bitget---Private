"""The coin registry: every purchase coin with its category, display name, the
address-format family its withdrawals use (None = never withdrawn) and whether
withdraw mode may ever be used for it.

Chain names for withdrawals are the exchange's own (see api/utils/chains.py);
the family only says which validator checks an address. USDT is NOT a purchase
coin: purchases are paid in USDT and Bitget has no USDTUSDT pair. USDC stays
because USDCUSDT exists and was online on Bitget when checked (2026-09-13).

Categories carry the policy, so a coin added to "meme" inherits it: meme coins
are accumulate-only (bought and held on the exchange, no withdraw mode even for
allowlisted users) and show a neutral risk note in the purchase form. Labels
describe; they never rank or recommend.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    accumulate_only: bool = False
    risk_note: Optional[str] = None


CATEGORIES: Dict[str, Category] = {
    "main": Category("main", "Main coins"),
    "stable": Category("stable", "Stablecoins"),
    "meme": Category("meme", "Meme coins", accumulate_only=True,
                     risk_note="Meme coins can move very sharply in price. Only buy what you can afford to lose."),
}


@dataclass(frozen=True)
class Coin:
    symbol: str
    name: str
    category: str
    family: Optional[str] = None   # address family for withdrawals; None = accumulate-only

    @property
    def withdrawable(self) -> bool:
        return self.family is not None and not CATEGORIES[self.category].accumulate_only


COINS: Dict[str, Coin] = {c.symbol: c for c in (
    Coin("BTC", "Bitcoin", "main", "bitcoin"),
    Coin("ETH", "Ethereum", "main", "ethereum"),
    Coin("USDC", "USD Coin", "stable", "ethereum"),   # delivered as an ERC-20 token by default
    Coin("SOL", "Solana", "main", "solana"),
    Coin("XRP", "XRP", "main", "xrpl"),               # native XRP on the XRP Ledger (Bitget chain "XRP"); destination tag on exchanges
    Coin("XLM", "Stellar", "main", "stellar"),        # native XLM on Stellar (Bitget chain "StellarLumens"); memo on exchanges
    Coin("DOGE", "Dogecoin", "meme"),                 # Bitget DOGEUSDT checked online 2026-10-02; accumulate-only
    Coin("SHIB", "Shiba Inu", "meme"),                # Bitget SHIBUSDT checked online 2026-10-02; accumulate-only
    Coin("PEPE", "Pepe", "meme"),                     # Bitget PEPEUSDT checked online 2026-10-02; whole-coin pair; accumulate-only
)}

SUPPORTED_CRYPTOS = tuple(COINS)
CRYPTO_CHAINS: Dict[str, str] = {s: c.family for s, c in COINS.items() if c.family}   # withdrawable coins -> family
ACCUMULATE_ONLY = frozenset(s for s, c in COINS.items() if not c.withdrawable)


def coin_spec(symbol: str) -> Optional[Coin]:
    return COINS.get((symbol or "").upper())


def category_of(symbol: str) -> Optional[Category]:
    c = coin_spec(symbol)
    return CATEGORIES[c.category] if c else None


def is_withdrawable(symbol: str) -> bool:
    c = coin_spec(symbol)
    return bool(c and c.withdrawable)


def categories_with_coins() -> List[dict]:
    """[{category, label, accumulate_only, risk_note, coins:[{symbol, name}]}] in registry order."""
    out = []
    for key, cat in CATEGORIES.items():
        coins = [{"symbol": c.symbol, "name": c.name} for c in COINS.values() if c.category == key]
        if coins:
            out.append({"category": key, "label": cat.label, "accumulate_only": cat.accumulate_only,
                        "risk_note": cat.risk_note, "coins": coins})
    return out
