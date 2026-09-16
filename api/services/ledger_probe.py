"""Pre-send typo detector for tag chains: ask the public ledger whether the
destination account exists. An unfunded destination is normal for a brand-new
self-custody wallet (Bitget's minimums exceed both reserves, so the first send
funds it) but a mistyped address looks exactly the same — so this WARNS, never
blocks. Both signals were verified against the live nodes on 2026-09-16:
XRPL account_info answers actNotFound, Horizon answers 404.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("autobuy.ledger_probe")

XRPL_RPC = "https://s1.ripple.com:51234/"
HORIZON = "https://horizon.stellar.org/accounts/{address}"
TIMEOUT = 8


def destination_funded(chain_family: str, address: str) -> dict:
    """{funded: True | False | None, detail}. None = the ledger could not be asked."""
    try:
        if chain_family == "xrpl":
            req = urllib.request.Request(XRPL_RPC, data=json.dumps({"method": "account_info", "params": [
                {"account": address, "ledger_index": "validated"}]}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                result = json.load(r).get("result", {})
            if result.get("error") == "actNotFound":
                return {"funded": False, "detail": "This address has never received XRP on the XRP Ledger."}
            if result.get("account_data"):
                return {"funded": True, "detail": "Account exists on the XRP Ledger."}
            return {"funded": None, "detail": f"XRP Ledger answered {result.get('error') or 'unexpectedly'}"}
        if chain_family == "stellar":
            req = urllib.request.Request(HORIZON.format(address=address), headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT):
                    return {"funded": True, "detail": "Account exists on Stellar."}
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return {"funded": False, "detail": "This address has never received XLM on Stellar."}
                return {"funded": None, "detail": f"Horizon answered {exc.code}"}
    except Exception as exc:
        logger.warning("ledger probe unavailable for %s: %s", chain_family, exc)
        return {"funded": None, "detail": "The ledger could not be reached; existence not checked."}
    return {"funded": None, "detail": "No ledger probe for this chain."}


def unfunded_warning(chain_family: str, address: str, coin: str) -> str | None:
    """The warning text shown under the address when the destination has never received funds."""
    probe = destination_funded(chain_family, address)
    if probe["funded"] is False:
        return (f"This address has never received {coin}. That is normal for a brand-new wallet, but a wrong "
                f"character also looks like this. Double-check it before saving.")
    return None
