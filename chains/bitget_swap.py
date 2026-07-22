"""Buy a token with USDT through the Bitget Wallet Trading API ("SwapX").

Docs: https://web3.bitget.com/en/docs/trading/overview
API keys: https://portal-web3.bitget.com  (application has a BD/contact step)

    ############################################################
    #  THIS MODULE HAS NEVER BEEN RUN AGAINST THE LIVE API.    #
    #  It is written to the published documentation only.      #
    #  No request in it has ever received a real response, so  #
    #  EVERY endpoint path, field name, envelope shape and     #
    #  signing detail below is PROVISIONAL.                    #
    ############################################################

What SwapX is, and is not
    It is a SELF-CUSTODY DEX AGGREGATOR.  It returns *unsigned* transaction
    data; this process signs it with its own private key.  Bitget never holds
    the key or the funds, and SwapX **cannot touch a Bitget exchange balance** -
    it is not a CEX order route and is not interchangeable with the spot bot.

    The one genuine operational advantage over ``bsc_swap``/``solana_swap`` is
    "NoGas": Bitget sponsors gas (EIP-7702 style delegation on EVM, where the
    wallet signs EIP-712 typed data instead of a normal transaction; an internal
    feePayer on Solana), which removes the need to keep a BNB/SOL float.  It
    requires a STABLECOIN input and a minimum order value of about $5.

Everything uncertain is CONFIGURABLE rather than guessed silently:
    * ``BitgetSwapClient.base_url``  - default host, overridable.
    * ``PATHS``   - endpoint paths.
    * ``WIRE``    - every request/response field name, with aliases.  When the
      API is first exercised for real, correct THIS DICT, not the call sites.
    * ``BitgetSwapClient._auth_headers`` - the whole HMAC recipe, in one method.

Second host worth knowing about: ``https://copenapi.bgwapi.io`` is what Bitget's
own MIT-licensed tools (bitget-wallet-mcp / -cli / -skill) call with NO API key
("SHA256 hash signing, BKHmacAuth").  It is a client-facing endpoint, may change
without notice, and its ToS position for unattended bot use is unclear - so it
is documented here and NOT the default.

House rules kept from the sibling back-ends: dry_run defaults True and stops
before ANY signing or submission; every failure is fail-closed; the token is
identified by ADDRESS/MINT only (symbols are attacker-controlled metadata and
are never resolved); key material never reaches a log line.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

import requests

from .errors import ChainError, InsufficientGas, QuoteStale, SwapReverted  # noqa: F401
from .wallet_keys import SecretKey, load_secret_key

LOG = logging.getLogger(__name__)

BACKEND = "bitget_swapx"

DEFAULT_BASE_URL = "https://bopenapi.bgwapi.io"
# Keyless client-facing host used by Bitget's own open-source tools. Not a
# default: undocumented stability, unclear ToS for unattended bots.
CLIENT_BASE_URL = "https://copenapi.bgwapi.io"

# UNVERIFIED paths, from the Order Mode flow in the docs.
PATHS = {
    "quote": "/bgw-pro/swapx/quote",
    "make_order": "/bgw-pro/swapx/order/makeSwapOrder",
    "submit_order": "/bgw-pro/swapx/order/submitSwapOrder",
    "get_order": "/bgw-pro/swapx/order/getSwapOrder",
}

CHAIN_BSC = "bsc"
CHAIN_SOLANA = "solana"
SUPPORTED_CHAINS = (CHAIN_BSC, CHAIN_SOLANA)

# UNVERIFIED: Bitget/OKX-family aggregators index BNB Smart Chain by its EVM
# chain id and Solana as "501". Corrected in one place if wrong.
CHAIN_INDEX = {CHAIN_BSC: "56", CHAIN_SOLANA: "501"}

# BSC USDT has 18 decimals, NOT 6. Solana USDT has 6.
USDT = {
    CHAIN_BSC: "0x55d398326f99059fF775485246999027B3197955",
    CHAIN_SOLANA: "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
USDT_DECIMALS = {CHAIN_BSC: 18, CHAIN_SOLANA: 6}
KEY_ENV = {CHAIN_BSC: "BSC_PRIVATE_KEY_FILE", CHAIN_SOLANA: "SOLANA_PRIVATE_KEY_FILE"}

DEFAULT_SLIPPAGE_BPS = 100
MAX_SLIPPAGE_BPS = 5000          # 50%; matches bsc_swap/solana_swap
DEFAULT_TIMEOUT = 20
# UNVERIFIED: docs say NoGas needs a stablecoin input and roughly $5 of order
# value. USDT is treated as ~1 USD for this gate only.
GASLESS_MIN_USDT = Decimal("5")
DEFAULT_POLL_ATTEMPTS = 3
DEFAULT_POLL_SECONDS = 2

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Base58, Bitcoin alphabet (no 0 O I l). Rejects Cyrillic homoglyphs outright.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_ZERO = "0x" + "0" * 40

# ---------------------------------------------------------------------------
# WIRE: every provisional field name lives here. First alias is what we send;
# reads try each alias in order. This is the single place to correct once the
# API has actually been called.
# ---------------------------------------------------------------------------
WIRE = {
    "envelope_code": ("code", "errorCode", "status"),
    "envelope_msg": ("msg", "message", "errorMsg"),
    "envelope_data": ("data", "result"),
    "success_codes": ("00000", "0", 0, 200, "success", "SUCCESS"),
    "order_id": ("orderId", "swapOrderId", "id"),
    "in_amount": ("fromTokenAmount", "inAmount", "amount"),
    "expected_out": ("toTokenAmount", "outAmount", "expectedOut"),
    "min_out": ("minReceiveAmount", "otherAmountThreshold", "amountOutMin"),
    "route": ("dexRouterList", "routePlan", "router"),
    "price_impact": ("priceImpactPercentage", "priceImpactPct", "priceImpact"),
    # EVM, normal mode: a transaction object to sign as-is.
    "evm_tx": ("tx", "transaction", "txData"),
    # EVM, NoGas mode: EIP-712 typed data to sign instead of a transaction.
    "typed_data": ("typedData", "eip712", "typedDataV4"),
    # Solana: a base64 serialized (versioned) transaction.
    "solana_tx": ("transaction", "serializedTx", "swapTransaction"),
    # What we hand back on submit.
    "signed_txs": ("signedTxs", "signedTransactions"),
    "tx_hash": ("txHash", "transactionHash", "hash", "signature"),
    "order_status": ("status", "orderStatus", "state"),
    "success_order_status": ("success", "SUCCESS", "filled", "FILLED", "done"),
    "failed_order_status": ("fail", "FAIL", "failed", "FAILED", "reverted"),
}


# ---------- errors ----------

class BitgetSwapError(ChainError):
    """Anything SwapX refuses or fails at. Carries the venue's own error code.

    ``venue_code`` is what Bitget returned (e.g. ``"40037"``); ``code`` is our
    own uppercase slug, so callers can branch on either. ``signed`` says whether
    a signature had already been produced when this was raised - see
    :class:`BitgetSwapUnconfirmed`.
    """

    exchange = BACKEND
    signed = False

    def __init__(self, code, message, http_status=None, venue_code=None):
        super().__init__(code, message, http_status)
        self.venue_code = venue_code if venue_code is not None else code


class BitgetSwapUnconfirmed(BitgetSwapError):
    """We signed, then lost the submit. The trade MAY be on chain.

    Never retry blind: reconcile ``exc.order_id`` via ``get_order`` (or the
    hash on chain) first. Every error raised BEFORE signing is a plain
    :class:`BitgetSwapError` with ``signed is False``, which means definitely
    not sent and safe to retry.
    """

    signed = True

    def __init__(self, code, message, http_status=None, venue_code=None,
                 order_id=None, tx_hash=None):
        super().__init__(code, message, http_status, venue_code)
        self.order_id = order_id
        self.tx_hash = tx_hash


# Dual bases on purpose: chain-agnostic callers keep catching QuoteStale /
# SwapReverted / InsufficientGas exactly as they do for the other back-ends,
# while a SwapX-specific handler catches BitgetSwapError and reads .venue_code.
class BitgetQuoteStale(BitgetSwapError, QuoteStale):
    """No usable route/quote, or the quote aged out."""


class BitgetSwapReverted(BitgetSwapError, SwapReverted):
    """The venue says the swap failed or would fail."""


class BitgetInsufficientGas(BitgetSwapError, InsufficientGas):
    """Not enough native coin (or the NoGas sponsorship was refused)."""


# ---------- client ----------

class BitgetSwapClient:
    """Thin SwapX HTTP wrapper. No wallet key material ever reaches it."""

    # UNVERIFIED header names / signing recipe - all of it lives in
    # _auth_headers so it can be corrected in exactly one place.
    HEADER_API_KEY = "x-api-key"
    HEADER_SIGN = "x-api-sign"
    HEADER_TIMESTAMP = "x-api-timestamp"
    SIGN_ENCODING = "hex"        # "hex" or "base64"
    SIGN_PARTS = ("timestamp", "method", "path", "body")

    def __init__(self, base_url: Optional[str] = None, api_key=None, api_secret=None,
                 timeout: int = DEFAULT_TIMEOUT, gasless: bool = False,
                 session=None, paths: Optional[dict] = None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.gasless = bool(gasless)
        self.paths = dict(PATHS, **(paths or {}))
        for name, value in (("api_key", api_key), ("api_secret", api_secret)):
            if value is not None and not isinstance(value, SecretKey):
                raise BitgetSwapError(
                    "KEY_TYPE", f"{name} must be a SecretKey (see wallet_keys), "
                                f"not raw {type(value).__name__}")
        self._api_key = api_key
        self._api_secret = api_secret
        self._session = session or requests.Session()

    # ---------- the one place the auth recipe lives ----------

    def _auth_headers(self, method: str, path: str, body_text: str) -> Dict[str, str]:
        """Assemble auth headers. UNVERIFIED - correct the recipe HERE only.

        Documented as ``x-api-key`` plus HMAC-SHA256. The prehash string order,
        the header names and the digest encoding are all guesses modelled on
        Bitget's CEX v2 scheme (timestamp + METHOD + path + body). With no
        secret configured only the key header is sent, which is also what the
        keyless ``copenapi`` host expects.
        """
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key is None:
            return headers
        headers[self.HEADER_API_KEY] = self._api_key.reveal()
        if self._api_secret is None:
            return headers
        timestamp = str(int(time.time() * 1000))
        parts = {"timestamp": timestamp, "method": method.upper(),
                 "path": path, "body": body_text or ""}
        prehash = "".join(parts[p] for p in self.SIGN_PARTS)
        digest = hmac.new(self._api_secret.reveal().encode("utf-8"),
                          prehash.encode("utf-8"), hashlib.sha256)
        headers[self.HEADER_SIGN] = (digest.hexdigest() if self.SIGN_ENCODING == "hex"
                                     else base64.b64encode(digest.digest()).decode("ascii"))
        headers[self.HEADER_TIMESTAMP] = timestamp
        return headers

    # ---------- transport ----------

    def _request(self, method: str, path: str, params=None, body=None) -> dict:
        body_text = json.dumps(body, separators=(",", ":"), sort_keys=True) if body else ""
        try:
            resp = self._session.request(
                method, f"{self.base_url}{path}", params=params, json=body,
                headers=self._auth_headers(method, path, body_text), timeout=self.timeout)
        except Exception as exc:                      # network/timeout -> fail closed
            raise BitgetSwapError("NETWORK", f"{method} {path}: {exc}") from exc

        status = getattr(resp, "status_code", None)
        try:
            payload = resp.json()
        except Exception:
            raise BitgetSwapError("BAD_RESPONSE", f"{method} {path}: non-JSON body "
                                  f"{getattr(resp, 'text', '')[:200]!r}", status) from None
        if not isinstance(payload, dict):
            raise BitgetSwapError("BAD_RESPONSE", f"{method} {path}: expected an object, "
                                  f"got {type(payload).__name__}", status)
        venue_code = _first(payload, "envelope_code")
        message = str(_first(payload, "envelope_msg") or payload)
        if status == 429:
            raise BitgetSwapError("RATE_LIMITED", f"{method} {path}: rate limited",
                                  status, venue_code)
        if status is not None and status != 200:
            raise classify_error(venue_code or status, f"{method} {path}: {message}", status)
        # A business-code failure inside an HTTP 200 body is still a failure.
        if venue_code is not None and venue_code not in WIRE["success_codes"]:
            raise classify_error(venue_code, f"{method} {path}: {message}", status)
        data = _first(payload, "envelope_data")
        if data is None:                              # absent data is never "safe"
            raise BitgetSwapError("NO_DATA", f"{method} {path}: response carries no data "
                                  f"member ({list(payload)[:6]})", status, venue_code)
        if not isinstance(data, dict):
            raise BitgetSwapError("BAD_DATA", f"{method} {path}: data is "
                                  f"{type(data).__name__}, expected an object", status, venue_code)
        return data

    # ---------- public ----------

    def quote(self, chain: str, from_token: str, to_token: str,
              amount_base_units: int, slippage_bps: int, taker: str) -> dict:
        """Read-only price/route lookup. Sends nothing signable."""
        return self._request("GET", self.paths["quote"], params=self._swap_params(
            chain, from_token, to_token, amount_base_units, slippage_bps, taker))

    def make_order(self, chain: str, from_token: str, to_token: str,
                   amount_base_units: int, slippage_bps: int, taker: str,
                   recipient: Optional[str] = None) -> dict:
        """Ask SwapX to build the UNSIGNED transaction data for this swap."""
        body = self._swap_params(chain, from_token, to_token, amount_base_units,
                                 slippage_bps, taker)
        if recipient:
            body["receiver"] = recipient
        return self._request("POST", self.paths["make_order"], body=body)

    def submit_order(self, signed, order_id: Optional[str] = None) -> dict:
        """Hand the signed payload back. AFTER THIS THE MONEY MAY MOVE."""
        body = {WIRE["signed_txs"][0]: list(signed) if isinstance(signed, (list, tuple))
                else [signed]}
        if order_id:
            body[WIRE["order_id"][0]] = order_id
        return self._request("POST", self.paths["submit_order"], body=body)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", self.paths["get_order"],
                             params={WIRE["order_id"][0]: order_id})

    def _swap_params(self, chain, from_token, to_token, amount_base_units,
                     slippage_bps, taker) -> dict:
        params = {
            "chainIndex": CHAIN_INDEX[chain],
            "fromTokenAddress": from_token,
            "toTokenAddress": to_token,
            "amount": str(int(amount_base_units)),
            # UNVERIFIED unit: sent as a decimal fraction ("0.01" == 1%), which
            # is what the OKX/Bitget aggregator family documents.
            "slippage": _bps_to_fraction(slippage_bps),
            "userWalletAddress": taker,
        }
        if self.gasless:
            params["gasless"] = "true"
        return params


def classify_error(venue_code, message, http_status=None) -> BitgetSwapError:
    """Map a venue failure onto a typed error. Returns (never raises).

    The buckets are substring-based on purpose: the real code table is not
    published in a form we can rely on, and a wrong bucket must never turn a
    failure into a pass.  Anything unrecognised stays a plain BitgetSwapError.
    """
    blob = f"{venue_code} {message}".lower()
    if any(m in blob for m in ("no route", "no_route", "not tradable", "insufficient liquidity",
                               "route not found", "quote expired", "price expired")):
        return BitgetQuoteStale("NO_ROUTE", message, http_status, venue_code)
    if any(m in blob for m in ("slippage", "reverted", "simulation failed", "execution reverted")):
        return BitgetSwapReverted("REVERTED", message, http_status, venue_code)
    if any(m in blob for m in ("insufficient gas", "insufficient native", "out of gas",
                               "gas fee", "below the minimum order")):
        return BitgetInsufficientGas("GAS", message, http_status, venue_code)
    return BitgetSwapError("VENUE_ERROR", message, http_status, venue_code)


# ---------- validation ----------

def _decimal(amount) -> Decimal:
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        raise BitgetSwapError("BAD_AMOUNT", f"{amount!r} is not a number") from None
    if not value.is_finite():
        raise BitgetSwapError("BAD_AMOUNT", f"usdt_amount must be finite, got {amount!r}")
    return value


def _to_base_units(amount, decimals: int) -> int:
    value = _decimal(amount)
    if value <= 0:
        raise BitgetSwapError("BAD_AMOUNT", f"usdt_amount must be > 0, got {amount!r}")
    atomic = int(value.scaleb(int(decimals)).to_integral_value(rounding="ROUND_DOWN"))
    if atomic <= 0:
        raise BitgetSwapError("BAD_AMOUNT",
                              f"{value} rounds to 0 base units at {decimals} decimals")
    return atomic


def _bps_to_fraction(slippage_bps: int) -> str:
    return str((Decimal(int(slippage_bps)) / Decimal(10_000)).normalize())


def validate_slippage(slippage_bps) -> int:
    """Whole bps inside a sane band. A float here silently widens the floor."""
    if isinstance(slippage_bps, bool) or not isinstance(slippage_bps, int):
        raise BitgetSwapError("SLIPPAGE_TYPE", "slippage_bps must be a whole int, got "
                              f"{type(slippage_bps).__name__} {slippage_bps!r}")
    if not 0 < slippage_bps <= MAX_SLIPPAGE_BPS:
        raise BitgetSwapError("SLIPPAGE_RANGE",
                              f"slippage_bps {slippage_bps} outside 1..{MAX_SLIPPAGE_BPS}")
    return slippage_bps


def validate_token(chain: str, token: Any) -> str:
    """Assert the buy target is an ADDRESS/MINT. Symbols are never resolved.

    Token name and symbol are attacker-controlled metadata - a live Solana mint
    exists whose symbol is "USDT" in Cyrillic homoglyphs - so anything that is
    not a well-formed on-chain identifier is rejected outright.
    """
    if not isinstance(token, str) or not token:
        raise BitgetSwapError("BAD_TOKEN", "token identifier must be a non-empty string, "
                              f"got {type(token).__name__}")
    if chain == CHAIN_BSC:
        if not _EVM_ADDRESS_RE.match(token):
            raise BitgetSwapError("BAD_TOKEN", f"{token!r} is not a 20-byte 0x EVM address. "
                                  "Tokens are never resolved by symbol.")
        if token.lower() == _EVM_ZERO:
            raise BitgetSwapError("BAD_TOKEN", "token address is the zero address")
    else:
        if not _BASE58_RE.match(token):
            raise BitgetSwapError("BAD_TOKEN", f"{token!r} is not a base58 mint address "
                                  "(32-44 chars). Tokens are never resolved by symbol.")
    if token.lower() == USDT[chain].lower():
        raise BitgetSwapError("SAME_TOKEN", f"output token equals input USDT on {chain}")
    return token


def resolve_chain(chain=None, token_address=None, mint=None) -> str:
    """Explicit ``chain`` wins; otherwise infer from which identifier was given."""
    if chain is not None:
        name = str(chain).strip().lower()
        if name not in SUPPORTED_CHAINS:
            raise BitgetSwapError("BAD_CHAIN", f"chain {chain!r} is not one of "
                                  f"{SUPPORTED_CHAINS}")
        return name
    if token_address and mint:
        raise BitgetSwapError("BAD_CHAIN", "both token_address and mint were given and no "
                              "chain= to disambiguate; refusing to guess the chain")
    if token_address:
        return CHAIN_BSC
    if mint:
        return CHAIN_SOLANA
    raise BitgetSwapError("BAD_CHAIN", "no chain, token_address or mint given")


# ---------- signing ----------

def _resolve_key(key, chain: str, key_env: Optional[str]) -> SecretKey:
    """Lazily load the signing key. A dry run never reaches this function."""
    if key is not None:
        if not isinstance(key, SecretKey):
            raise BitgetSwapError("KEY_TYPE", "key must be a SecretKey, not raw material")
        return key
    fmt = "evm" if chain == CHAIN_BSC else "base58"
    return load_secret_key(key_env or KEY_ENV[chain], fmt=fmt)


def _sign_evm(key: SecretKey, order: dict, gasless: bool) -> str:
    """Sign the EVM payload. NoGas signs EIP-712 typed data, not a transaction.

    ``eth_account`` is imported here, not at module scope, so this file stays
    importable on a host without the web3 stack.
    """
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - depends on host env
        raise BitgetSwapError("DEP_MISSING", f"eth_account is required: {exc}") from None

    if gasless:
        typed = _first(order, "typed_data")
        if not isinstance(typed, dict):
            raise BitgetSwapError("MISSING_FIELD", "gasless order carries no EIP-712 typed "
                                  f"data under any of {WIRE['typed_data']}")
        payload, kind = typed, "typed data"
    else:
        payload = _first(order, "evm_tx")
        if not isinstance(payload, dict):
            raise BitgetSwapError("MISSING_FIELD", "order carries no transaction object "
                                  f"under any of {WIRE['evm_tx']}")
        kind = "transaction"

    # Nothing the signer raises is re-raised: a signer handed key material can
    # echo it into the message, and "from None" stops Python reprinting the
    # original under "During handling of the above exception".
    try:
        if gasless:
            signed = Account.sign_typed_data(key.reveal(), full_message=payload)
        else:
            signed = Account.sign_transaction(payload, private_key=key.reveal())
    except Exception as exc:
        raise BitgetSwapError("SIGN_FAILED", f"could not sign the {kind} "
                              f"({type(exc).__name__}); the reason is withheld because the "
                              "signer was handed key material") from None
    raw = (getattr(signed, "raw_transaction", None)
           or getattr(signed, "rawTransaction", None)
           or getattr(signed, "signature", None))
    if raw is None:
        raise BitgetSwapError("SIGN_FAILED", "signer returned no raw transaction/signature")
    return raw if isinstance(raw, str) else "0x" + bytes(raw).hex()


def _sign_solana(key: SecretKey, order: dict) -> str:
    """Reconstruct-and-sign: solders' VersionedTransaction is immutable."""
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
    except ImportError as exc:  # pragma: no cover - depends on host env
        raise BitgetSwapError("DEP_MISSING", f"solders is required: {exc}") from None

    tx_b64 = _first(order, "solana_tx")
    if not isinstance(tx_b64, str) or not tx_b64:
        raise BitgetSwapError("MISSING_FIELD", "order carries no serialized transaction "
                              f"under any of {WIRE['solana_tx']}")
    keypair = text = None
    try:
        text = key.reveal().strip()
        keypair = (Keypair.from_bytes(bytes(json.loads(text))) if text.startswith("[")
                   else Keypair.from_base58_string(text))
        text = None
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        return base64.b64encode(bytes(VersionedTransaction(raw_tx.message, [keypair]))).decode()
    except Exception:
        # `text` IS the secret and solders renders a Keypair's bytes under repr;
        # both are locals of this frame, which lands in any new exception's
        # traceback. Drop them before raising.
        keypair = text = None
        raise BitgetSwapError("SIGN_FAILED", "the order transaction could not be "
                              "reconstructed and signed") from None


def sign_order(chain: str, key: SecretKey, order: dict, gasless: bool) -> str:
    """Chain-appropriate signer. The ONLY place key material is touched."""
    if chain == CHAIN_BSC:
        return _sign_evm(key, order, gasless)
    return _sign_solana(key, order)


# ---------- helpers ----------

def _first(obj: Any, wire_key: str, default=None):
    """First present, non-empty value among ``WIRE[wire_key]`` aliases."""
    if not isinstance(obj, dict):
        return default
    for alias in WIRE[wire_key]:
        if alias in obj and obj[alias] not in (None, ""):
            return obj[alias]
    return default


def _require(order: dict, wire_key: str, what: str):
    value = _first(order, wire_key)
    if value is None:
        raise BitgetSwapError("MISSING_FIELD", f"{what} response has no usable {wire_key} "
                              f"(tried {WIRE[wire_key]}) - refusing to proceed")
    return value


def _as_int(value, what: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise BitgetSwapError("BAD_RESPONSE", f"{what} is not an integer: {value!r}") from None


def _after_signing(exc: BitgetSwapError) -> BitgetSwapError:
    """Mark an error as raised once a signature existed.

    ``signed is False`` is the caller's licence to retry ("definitely not
    sent"), so every failure past ``sign_order`` must set this - including the
    ones that are not :class:`BitgetSwapUnconfirmed`.
    """
    exc.signed = True
    return exc


# ---------- public ----------

def buy_token(usdt_amount=None, slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
              dry_run: bool = True, chain: Optional[str] = None,
              token_address: Optional[str] = None, mint: Optional[str] = None,
              token_mint: Optional[str] = None, recipient: Optional[str] = None,
              taker: Optional[str] = None, rpc_url: Optional[str] = None,
              min_native_balance=None, client: Optional[BitgetSwapClient] = None,
              key: Optional[SecretKey] = None, *, base_url: Optional[str] = None,
              api_key: Optional[SecretKey] = None, api_secret: Optional[SecretKey] = None,
              gasless: bool = False, key_env: Optional[str] = None,
              timeout: int = DEFAULT_TIMEOUT, session=None,
              poll_attempts: int = DEFAULT_POLL_ATTEMPTS,
              poll_seconds: int = DEFAULT_POLL_SECONDS, sleep=time.sleep) -> Dict[str, Any]:
    """Swap ``usdt_amount`` USDT for a token via Bitget SwapX. Dry-run by default.

    Signature-compatible with ``chains.bsc_swap.buy_token`` and
    ``chains.solana_swap.buy_token`` so ``memebuy.orchestrator`` can swap one
    back-end for another. BSC is selected by ``token_address`` (+ ``recipient``),
    Solana by ``mint``/``token_mint`` (+ ``taker``); an explicit ``chain=`` wins.

    Returns ``{backend: "bitget_swapx", status, chain, tx_hash, signature,
    amount_in/in_amount, amount_out_min, expected_out/out_amount, order_id,
    gasless, route, ...}``. ``status`` is ``"dry_run"`` or ``"success"``; a
    partial result is never returned.

    Order of operations: validate -> quote (read-only) -> **dry_run returns the
    plan HERE** -> makeSwapOrder -> sign -> submitSwapOrder -> poll getSwapOrder.

    CALLER CONTRACT: an exception with ``exc.signed is False`` was raised before
    any signature existed - definitely not sent, safe to retry. ``exc.signed is
    True`` means a signature existed: :class:`BitgetSwapUnconfirmed` if the
    outcome is unknown (reconcile ``exc.order_id`` before ANY retry or you may
    buy twice), :class:`BitgetSwapReverted` if the venue reports it failed.

    ``rpc_url`` is accepted and unused (SwapX needs no node of our own).
    ``min_native_balance`` cannot be enforced without an RPC: with ``gasless``
    on there is nothing to enforce, and with it off the caller keeps that duty -
    the result says so via ``native_balance_checked``.
    """
    # `if dry_run:` alone reads None/0/"" as LIVE. A money mover never guesses.
    if not isinstance(dry_run, bool):
        raise BitgetSwapError("BAD_DRY_RUN", f"dry_run must be a bool, got "
                              f"{type(dry_run).__name__} {dry_run!r} - refusing to guess "
                              "whether this is a live trade")
    chain = resolve_chain(chain, token_address, mint or token_mint)
    token = validate_token(chain, token_address if chain == CHAIN_BSC else (mint or token_mint))
    slippage_bps = validate_slippage(slippage_bps)
    if usdt_amount is None:
        raise BitgetSwapError("BAD_AMOUNT", "no usdt_amount given")

    poll_attempts = max(1, _as_int(poll_attempts, "poll_attempts"))

    # An injected client carries its own gasless setting; the keyword can only
    # turn it ON, never quietly off (that would drop the $5 gate).
    client_gasless = bool(getattr(client, "gasless", False))
    gasless = bool(gasless) or client_gasless
    if client is not None and gasless != client_gasless:
        # The CLIENT builds the order; this flag picks the SIGNER. Disagreeing
        # means signing EIP-712 typed data for an order built in normal mode -
        # a signature of the wrong kind, submitted as if it were right.
        raise BitgetSwapError("GASLESS_MISMATCH",
                              "gasless=True but the injected client is not gasless: it "
                              "would build a normal order and we would sign EIP-712 "
                              "typed data for it; configure gasless on the client")
    if gasless and _decimal(usdt_amount) < GASLESS_MIN_USDT:
        raise BitgetSwapError(
            "GASLESS_MINIMUM",
            f"NoGas needs an order of at least {GASLESS_MIN_USDT} USDT, got {usdt_amount}; "
            "below it Bitget will not sponsor the gas and the swap fails after signing")

    wallet = taker or recipient
    if not wallet:
        raise BitgetSwapError("NO_WALLET", "recipient (BSC) or taker (Solana) is required: "
                              "SwapX signs for a specific wallet address")
    if not isinstance(wallet, str):
        raise BitgetSwapError("BAD_WALLET", f"wallet must be a string, got "
                              f"{type(wallet).__name__}")
    if chain == CHAIN_BSC and not _EVM_ADDRESS_RE.match(wallet):
        raise BitgetSwapError("BAD_WALLET", f"{wallet!r} is not an EVM address")
    if chain == CHAIN_SOLANA and not _BASE58_RE.match(wallet):
        raise BitgetSwapError("BAD_WALLET", f"{wallet!r} is not a base58 address")

    if client is None:
        client = BitgetSwapClient(base_url=base_url, api_key=api_key, api_secret=api_secret,
                                  timeout=timeout, gasless=gasless, session=session)
    from_token = USDT[chain]
    amount_in = _to_base_units(usdt_amount, USDT_DECIMALS[chain])
    if min_native_balance and not gasless:
        LOG.warning("min_native_balance=%s cannot be enforced by the SwapX back-end (it "
                    "holds no RPC handle); the wallet must already hold gas, or use gasless",
                    min_native_balance)

    quote = client.quote(chain, from_token, token, amount_in, slippage_bps, wallet)
    expected_out = _as_int(_require(quote, "expected_out", "quote"), "quote expected_out")
    if expected_out <= 0:
        raise BitgetQuoteStale("QUOTE_ZERO",
                               f"quote returned a non-positive output {expected_out}")
    amount_out_min = expected_out * (10_000 - slippage_bps) // 10_000
    if amount_out_min <= 0:
        raise BitgetQuoteStale("QUOTE_ZERO", f"amountOutMin computed as 0 from {expected_out}")

    result = {
        "backend": BACKEND,
        "status": "dry_run" if dry_run else "pending",
        "dry_run": dry_run,
        "chain": chain,
        "gasless": gasless,
        "tx_hash": None,
        "signature": None,
        "order_id": None,
        "token": token,
        "input_token": from_token,
        "wallet": wallet,
        "amount_in": amount_in,
        "in_amount": amount_in,          # solana_swap's name for the same number
        "expected_out": expected_out,
        "out_amount": expected_out,      # ditto
        "amount_out_min": amount_out_min,
        "slippage_bps": slippage_bps,
        "price_impact": _first(quote, "price_impact"),
        "route": _first(quote, "route") or [],
        "native_balance_checked": False,
        "unverified_integration": True,  # this back-end has never met the live API
    }

    if dry_run:
        LOG.info("DRY-RUN SwapX buy: %s%s chain=%s %s -> %s amountIn=%s slippage=%sbps "
                 "gasless=%s expectedOut=%s amountOutMin=%s ; makeSwapOrder/submitSwapOrder "
                 "WOULD FOLLOW (not sent, nothing signed)", client.base_url,
                 client.paths["quote"], chain, from_token, token, amount_in, slippage_bps,
                 gasless, expected_out, amount_out_min)
        result["intended_call"] = {
            "endpoint": f"{client.base_url}{client.paths['make_order']}",
            "body": client._swap_params(chain, from_token, token, amount_in,
                                        slippage_bps, wallet),
        }
        return result

    # ----- past this line the trade is real -----
    order = client.make_order(chain, from_token, token, amount_in, slippage_bps,
                             wallet, recipient=recipient)
    order_id = _first(order, "order_id")
    result["order_id"] = order_id
    order_min = _first(order, "min_out")
    if order_min is not None and _as_int(order_min, "order min_out") < amount_out_min:
        # The order protects less than the quote we authorised.
        raise BitgetSwapError("MIN_RECEIVED", f"order's minimum received "
                              f"{order_min} is below the {amount_out_min} implied by the "
                              f"quote at {slippage_bps}bps - refusing to sign")

    signed = sign_order(chain, _resolve_key(key, chain, key_env), order, gasless)

    try:
        submitted = client.submit_order(signed, order_id=order_id)
    except Exception as exc:
        # Signed and handed over, or maybe not - we cannot tell from here.
        raise BitgetSwapUnconfirmed(
            "SUBMIT_UNCONFIRMED",
            f"submitSwapOrder failed AFTER the transaction was signed "
            f"({type(exc).__name__}: {exc}); Bitget may already have broadcast it - "
            f"reconcile order {order_id!r} before ANY retry",
            getattr(exc, "http_status", None), getattr(exc, "venue_code", None),
            order_id=order_id) from exc

    order_id = _first(submitted, "order_id") or order_id
    result["order_id"] = order_id
    tx_hash = _first(submitted, "tx_hash")
    if tx_hash is None:
        tx_hash = _poll_for_hash(client, order_id, poll_attempts, poll_seconds, sleep)

    result["status"] = "success"
    result["tx_hash"] = tx_hash          # bsc_swap's key
    result["signature"] = tx_hash        # solana_swap's key; same identifier
    filled = _first(submitted, "expected_out")
    if filled is not None:
        # The swap is submitted and its hash is in hand. Raising over a cosmetic
        # amount field would hand the caller a signed=False error - "definitely
        # not sent, safe to retry" - for a trade that already happened.
        try:
            result["out_amount"] = result["expected_out"] = _as_int(filled, "filled out")
        except BitgetSwapError:
            LOG.warning("SwapX order %s reported an unusable filled amount %r; keeping the "
                        "quoted %s (the trade itself is done, tx=%s)", order_id, filled,
                        result["expected_out"], tx_hash)
            result["out_amount_reported"] = filled
    LOG.info("SwapX buy filled: chain=%s order=%s tx=%s in=%s out=%s", chain, order_id,
             tx_hash, result["amount_in"], result["out_amount"])
    return result


def _poll_for_hash(client, order_id, attempts, seconds, sleep):
    """Poll getSwapOrder for the on-chain identifier. Silence is NOT success."""
    if not order_id:
        raise BitgetSwapUnconfirmed(
            "SUBMIT_NO_ORDER_ID", "submitSwapOrder returned neither a transaction hash nor an "
            "order id, so the trade cannot be reconciled; treat it as possibly broadcast")
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            state = client.get_order(order_id)
        except Exception as exc:
            LOG.warning("getSwapOrder %s failed (%s/%s): %s", order_id, attempt, attempts, exc)
            state = {}
        tx_hash = _first(state, "tx_hash")
        if tx_hash:
            return tx_hash
        status = str(_first(state, "order_status") or "").lower()
        if status in {str(s).lower() for s in WIRE["failed_order_status"]}:
            # A definite on-chain failure: nothing to reconcile, but the gas is
            # gone - and a signature DID exist, so say so.
            raise _after_signing(BitgetSwapReverted(
                "ORDER_FAILED", f"order {order_id} reports status {status!r}"))
        if attempt < attempts:
            sleep(seconds)
    raise BitgetSwapUnconfirmed(
        "FILL_UNCONFIRMED", f"order {order_id} was submitted but no transaction hash appeared "
        f"within {attempts} polls; it may still land - reconcile before ANY retry",
        order_id=order_id)
