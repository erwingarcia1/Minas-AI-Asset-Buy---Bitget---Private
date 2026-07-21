"""Solana chain client: Jupiter Swap API V2 + raw JSON-RPC helpers.

API docs:
  Jupiter Swap API V2 order/execute : https://dev.jup.ag/docs/swap-api/get-quote
  Jupiter execute endpoint          : https://dev.jup.ag/docs/swap-api/execute-swap
  Solana JSON-RPC                   : https://solana.com/docs/rpc/http

Notes verified live 2026-07:
  * V2 lives ONLY on https://api.jup.ag  (lite-api.jup.ag/swap/v2/* -> 404).
  * Keyless works at ~30 RPM; an ``x-api-key`` header raises the limit.
  * The order/execute path is RPC-less for the swap itself: Jupiter simulates,
    sets the priority fee and broadcasts.  We still need plain RPC for balance
    and mint-authority reads, hence the JSON-RPC helpers below.
  * ``simulationError`` MUST be checked before signing - see solana_swap.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from .errors import ChainError, chain_error_family

LOG = logging.getLogger(__name__)

# Built ONCE for the whole chain (solana_swap imports this one, it does not
# build a second family).  Raise SOL.X; callers still catch the shared bases.
SOL = chain_error_family("solana")

JUPITER_BASE_URL = "https://api.jup.ag"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

# Anchor error codes seen from Jupiter-routed swaps (decimal -> explanation).
_ANCHOR_MESSAGES = {
    6001: ("SlippageToleranceExceeded (0x1771) - price moved past slippageBps; "
           "re-quote, never resubmit"),
    6014: ("IncorrectTokenProgramID (0x177e) - mint belongs to a different SPL "
           "token program (e.g. Token-2022)"),
    6024: ("InsufficientFunds (0x1788) - balance does not cover amount + fees + "
           "ATA rent (2,039,280 lamports for a first-ever buy)"),
}
_HEX_TO_ANCHOR = {"0x1771": 6001, "0x177e": 6014, "0x1788": 6024}

# Substring markers used to bucket free-form API/on-chain error text.
# OUTCOME-UNKNOWN comes FIRST and is deliberately broad: it means "we handed the
# transaction over and do NOT know whether it landed", which is the one failure
# where re-quoting signs and broadcasts a SECOND spend while the first may still
# confirm.  It has to be tested before _STALE_MARKERS because the canonical text
# for this case - solana-py's TransactionExpiredTimeoutError, "Transaction was
# not confirmed in 30.00 seconds. It is unknown if it succeeded or failed." -
# also matches "transactionexpired"/"expired" and would otherwise be retried.
_UNKNOWN_MARKERS = ("unknown if it succeeded", "was not confirmed", "not confirmed in",
                    "timeouterror", "timed out", "timeout", "unknown status")
# Definitively dead: the transaction can never be included in a block, so a
# re-quote is safe.
_STALE_MARKERS = ("blockhashnotfound", "blockhash not found", "block height exceeded",
                  "transactionexpired", "expired", "slot has passed")
_REVERT_MARKERS = ("slippage", "custom program error", "insufficient",
                   "transaction failed", "simulation failed", "reverted")


# ---------- error mapping ----------

def _anchor_code(code: Any, message: str) -> Optional[int]:
    """Return the decimal Anchor code if `code`/`message` carries one."""
    if isinstance(code, int) and code in _ANCHOR_MESSAGES:
        return code
    blob = f"{code} {message}".lower()
    for hex_code, dec in _HEX_TO_ANCHOR.items():
        if hex_code in blob:
            return dec
    for dec in _ANCHOR_MESSAGES:
        if f"custom program error: {dec}" in blob or f'"{dec}"' in blob:
            return dec
    return None


def classify_error(code: Any, message: str, http_status: Optional[int] = None) -> ChainError:
    """Map a Jupiter/Anchor/RPC failure onto a typed, readable error.

    Returns (never raises) so callers read as ``raise classify_error(...)``.
    """
    anchor = _anchor_code(code, message)
    if anchor is not None:
        # An Anchor code only exists if the transaction executed: a definite outcome.
        return SOL.SwapReverted(anchor, f"{message} | {_ANCHOR_MESSAGES[anchor]}", http_status)
    blob = f"{code} {message}".lower()
    if any(m in blob for m in _UNKNOWN_MARKERS):
        # Plain ChainError ON PURPOSE - NOT QuoteStale.  buy_token only re-quotes
        # QuoteStale, and re-quoting an unknown outcome is a double spend.
        return SOL.ChainError("OUTCOME_UNKNOWN",
                              f"{message} | the transaction's fate is UNKNOWN; it may still "
                              "confirm. Do NOT re-quote or resubmit - check the signature "
                              "on-chain before any further spend", http_status)
    if any(m in blob for m in _STALE_MARKERS):
        return SOL.QuoteStale(code, f"{message} | blockhash/quote expired - re-quote", http_status)
    if "no_routes_found" in blob:
        return SOL.QuoteStale(code, f"{message} | no route for this pair/size", http_status)
    if "token_not_tradable" in blob:
        return SOL.QuoteStale(code, f"{message} | mint is not tradable on any "
                              "indexed venue", http_status)
    if any(m in blob for m in _REVERT_MARKERS):
        return SOL.SwapReverted(code, message, http_status)
    return SOL.ChainError(code, message, http_status)


class SolanaClient:
    """Thin Jupiter V2 + JSON-RPC wrapper.  No key material ever reaches it."""

    def __init__(self, base_url: str = JUPITER_BASE_URL, api_key: Optional[str] = None,
                 rpc_url: str = SOLANA_RPC_URL, timeout: int = 20,
                 session: Optional["requests.Session"] = None):
        self.base_url = base_url.rstrip("/")
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._api_key = api_key
        self._session = session or requests.Session()
        self._rpc_id = 0

    # ---------- private ----------

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:  # optional: lifts the ~30 RPM keyless rate limit
            headers["x-api-key"] = self._api_key
        return headers

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None) -> dict:
        """Call Jupiter, raise a typed error on any failure, return the payload."""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, params=params, json=body,
                                         headers=self._headers(), timeout=self.timeout)
        except Exception as exc:  # network/timeout -> fail closed
            raise SOL.ChainError("NETWORK", f"{method} {path}: {exc}") from exc

        status = getattr(resp, "status_code", None)
        try:
            payload = resp.json()
        except Exception:
            raise SOL.ChainError("BAD_RESPONSE", f"{method} {path}: non-JSON body "
                                 f"{getattr(resp, 'text', '')[:200]!r}", status) from None
        if status == 429:
            raise SOL.ChainError("RATE_LIMITED", f"{method} {path}: rate limited by Jupiter", status)
        if not isinstance(payload, dict):
            raise SOL.ChainError("BAD_RESPONSE", f"{method} {path}: expected an object, "
                                 f"got {type(payload).__name__}", status)
        if status != 200:
            raise classify_error(payload.get("errorCode") or payload.get("code") or status,
                                 str(payload.get("error") or payload.get("message") or payload), status)
        # Business-code failure inside a 200 body.
        if payload.get("error") or payload.get("errorCode"):
            raise classify_error(payload.get("errorCode", "ERROR"),
                                 str(payload.get("error", "")), status)
        return payload

    def _rpc(self, method: str, params: list) -> Any:
        """Solana JSON-RPC call; returns the `result` member or raises."""
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        try:
            resp = self._session.request("POST", self.rpc_url, json=body,
                                         headers={"Content-Type": "application/json"},
                                         timeout=self.timeout)
        except Exception as exc:
            raise SOL.ChainError("RPC_NETWORK", f"{method}: {exc}") from exc

        status = getattr(resp, "status_code", None)
        if status == 429:
            raise SOL.ChainError("RATE_LIMITED", f"rpc {method}: rate limited", status)
        try:
            payload = resp.json()
        except Exception:
            raise SOL.ChainError("RPC_BAD_RESPONSE", f"rpc {method}: non-JSON body", status) from None
        if status != 200 or not isinstance(payload, dict):
            raise SOL.ChainError("RPC_HTTP", f"rpc {method}: http {status}", status)
        if payload.get("error"):
            err = payload["error"]
            raise classify_error(err.get("code", "RPC_ERROR"), str(err.get("message", err)), status)
        if "result" not in payload:  # fail closed: absent data is never "safe"
            raise SOL.ChainError("RPC_NO_RESULT", f"rpc {method}: response has no 'result'", status)
        return payload["result"]

    # ---------- public: Jupiter ----------

    def get_order(self, input_mint: str, output_mint: str, amount_atomic: int,
                  slippage_bps: int, taker: str, restrict_intermediate_tokens: bool = True,
                  dynamic_compute_unit_limit: bool = True, wrap_and_unwrap_sol: bool = True,
                  extra_params: Optional[dict] = None) -> dict:
        """GET /swap/v2/order - quote + unsigned transaction + simulation result.

        restrict_intermediate_tokens defaults True: a live USDT->JUP quote routed
        3 hops through low-tier venues, which is where slippage failures come from.
        """
        if amount_atomic <= 0:
            raise SOL.ChainError("BAD_AMOUNT", f"amount_atomic must be > 0, got {amount_atomic}")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_atomic)),
            "slippageBps": str(int(slippage_bps)),
            "taker": taker,
            "restrictIntermediateTokens": "true" if restrict_intermediate_tokens else "false",
            "dynamicComputeUnitLimit": "true" if dynamic_compute_unit_limit else "false",
            "wrapAndUnwrapSol": "true" if wrap_and_unwrap_sol else "false",
        }
        if extra_params:
            params.update(extra_params)
        return self._request("GET", "/swap/v2/order", params=params)

    def execute_order(self, signed_tx_b64: str, request_id: str) -> dict:
        """POST /swap/v2/execute - Jupiter broadcasts and polls for confirmation."""
        payload = self._request("POST", "/swap/v2/execute",
                                body={"signedTransaction": signed_tx_b64, "requestId": request_id})
        status = payload.get("status")
        if status is None:  # fail closed - never read a missing status as success
            raise SOL.ChainError("NO_STATUS", f"execute response lacks 'status': {payload!r}")
        if str(status).lower() != "success":
            raise classify_error(payload.get("code", status), str(payload.get("error", payload)))
        if not payload.get("signature"):
            raise SOL.ChainError("NO_SIGNATURE",
                                 f"execute reported success without a signature: {payload!r}")
        return payload

    # ---------- public: RPC helpers ----------

    def get_sol_balance(self, pubkey: str) -> int:
        """Lamports held by `pubkey`.  Raises rather than returning 0 on doubt."""
        result = self._rpc("getBalance", [pubkey, {"commitment": "confirmed"}])
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if not isinstance(value, int):
            raise SOL.ChainError("NO_BALANCE", f"getBalance returned no integer value for {pubkey}")
        return value

    def get_token_balance(self, owner: str, mint: str) -> dict:
        """Summed SPL balance of `mint` held by `owner` (0 when no ATA exists)."""
        result = self._rpc("getTokenAccountsByOwner",
                           [owner, {"mint": mint},
                            {"encoding": "jsonParsed", "commitment": "confirmed"}])
        accounts = (result or {}).get("value") if isinstance(result, dict) else None
        if accounts is None:
            raise SOL.ChainError("NO_TOKEN_ACCOUNTS",
                                 f"getTokenAccountsByOwner returned no value for {owner}")
        total, decimals = 0, None
        for acct in accounts:
            data = ((acct or {}).get("account") or {}).get("data") or {}
            info = (data.get("parsed") or {}).get("info", {})
            amount = (info.get("tokenAmount") or {})
            if "amount" not in amount or "decimals" not in amount:
                raise SOL.ChainError("BAD_TOKEN_ACCOUNT",
                                     f"token account for {mint} lacks tokenAmount fields")
            total += int(amount["amount"])
            decimals = int(amount["decimals"])
        return {"mint": mint, "amount": total, "decimals": decimals,
                "accounts": len(accounts)}

    def get_mint_info(self, mint: str) -> dict:
        """Parsed mint account: decimals + authorities + owning token program.

        Every field is asserted present - a mint we cannot fully parse is treated
        as unsafe, not as "no authority set".
        """
        result = self._rpc("getAccountInfo",
                           [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if not value:
            raise SOL.ChainError("MINT_NOT_FOUND", f"mint account {mint} does not exist")
        parsed = ((value.get("data") or {}).get("parsed") or {})
        info = parsed.get("info") or {}
        if parsed.get("type") != "mint" or "decimals" not in info:
            raise SOL.ChainError("NOT_A_MINT", f"account {mint} is not a parsable SPL mint account")
        return {
            "mint": mint,
            "decimals": int(info["decimals"]),
            "mint_authority": info.get("mintAuthority"),      # None == renounced
            "freeze_authority": info.get("freezeAuthority"),  # None == cannot freeze
            "supply": info.get("supply"),
            "is_initialized": bool(info.get("isInitialized", False)),
            "program": value.get("owner"),                    # Tokenkeg... or Token-2022
        }
