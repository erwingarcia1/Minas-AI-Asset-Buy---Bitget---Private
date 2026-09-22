"""Which address-format family each purchase coin is delivered on.

Chain names for withdrawals are the exchange's own (see api/utils/chains.py);
this table only says which family validates the address."""
from __future__ import annotations

from typing import Dict

# USDC is delivered as an ERC-20 token by default, so it uses Ethereum addresses.
# USDT is NOT a purchase coin: purchases are paid in USDT and Bitget has no
# USDTUSDT pair. USDC stays because USDCUSDT exists and was online on Bitget
# when checked (2026-09-13).
CRYPTO_CHAINS: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDC": "ethereum",
    "SOL": "solana",
    "XRP": "xrpl",       # native XRP on the XRP Ledger (Bitget chain "XRP"); destination tag on exchanges
    "XLM": "stellar",    # native XLM on Stellar (Bitget chain "StellarLumens"); memo on exchanges
}

SUPPORTED_CRYPTOS = tuple(CRYPTO_CHAINS)
