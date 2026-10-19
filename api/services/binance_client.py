"""One-release alias for api/services/exchange_client.py (renamed 2026-09-22).

Nothing in this repository imports this path any more; it exists so an
external caller or an unmerged branch keeps working for one release. Patching
attributes here does NOT affect the real module: patch exchange_client.
"""
from api.services.exchange_client import *  # noqa: F401,F403
from api.services.exchange_client import (  # noqa: F401
    BITGET_EXTRA_TOKENS, LEGACY_CLIENTS, BinanceClient, BinanceClientError, ExchangeClient, ExchangeClientError,
    _LegacyClient, interpret_bitget_authorities,
)
