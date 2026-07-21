"""Buy an SPL token with USDT through the Jupiter aggregator (Solana).

API docs: https://dev.jup.ag/docs/swap-api/get-quote  (order -> sign -> execute)

Safety model (this module is the point of the subsystem):
  * dry_run=True is the DEFAULT everywhere; live requires an explicit opt-in.
  * Fail closed: a missing field in any safety check is a REJECT, never a pass.
  * Identity is the MINT ADDRESS ONLY.  Token name and symbol are attacker
    controlled - a live mint exists whose symbol is "USDT" and whose name
    renders as "Tether USD" using Cyrillic homoglyphs.  This API never accepts
    a symbol and never resolves one.
  * ``simulationError`` from the order response is checked BEFORE signing.  When
    simulation fails, dynamicComputeUnitLimit falls back to the 1,400,000 CU max
    and inflates the fee, so a "just send it" retry is expensive as well as wrong.
  * solders' VersionedTransaction is immutable: we reconstruct-and-sign
    (``VersionedTransaction(raw.message, [keypair])``), never sign in place.
    Pin solders==0.28.0, solana==0.40.1.
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from .errors import ChainError, QuoteStale, SwapReverted  # noqa: F401 (catchable bases)
from .solana_client import SOL  # chain-labelled ChainError/SwapReverted/... family
from .solana_client import SOLANA_RPC_URL, SolanaClient
from .wallet_keys import SecretKey, load_secret_key

LOG = logging.getLogger(__name__)

USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"   # 6 decimals
LAMPORTS_PER_SOL = 1_000_000_000
ATA_RENT_LAMPORTS = 2_039_280           # first-ever buy of a token, non-refundable
# Realistic floor is 0.03-0.05 SOL, NOT 0.01: ATA rent + priority fee + base fee.
DEFAULT_SOL_FLOOR_LAMPORTS = 40_000_000  # 0.04 SOL
DEFAULT_SLIPPAGE_BPS = 100               # 1%
DEFAULT_MAX_PRICE_IMPACT_PCT = Decimal("10")
MAX_REQUOTES = 2                         # bounded: blockhash dies after ~150 slots
# A floor under this cannot even pay ATA rent + one base fee, so a config that
# asks for less is clamped UP - failing closed harder than asked is always safe.
MIN_SANE_FLOOR_LAMPORTS = ATA_RENT_LAMPORTS + 5_000
KEY_ENV = "SOLANA_PRIVATE_KEY_FILE"      # holds a PATH; never the key itself

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ---------- private: validation ----------

def _b58_decode(text: str) -> bytes:
    """Minimal base58 decode.  Raises ValueError on any non-alphabet character
    (which is what rejects Cyrillic/homoglyph look-alikes)."""
    num = 0
    for char in text:
        idx = _B58_ALPHABET.find(char)
        if idx < 0:
            raise ValueError(f"invalid base58 character {char!r}")
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


def validate_mint(mint: Any, input_mint: str = USDT_MINT) -> str:
    """Assert `mint` is a real 32-byte base58 address and a legal buy target."""
    if not isinstance(mint, str) or not mint:
        raise SOL.ChainError("BAD_MINT", "mint must be a non-empty base58 string, "
                             f"got {type(mint).__name__}")
    if len(mint) < 32 or len(mint) > 44:
        # Symbols ("JUP", "USDT", "BONK") land here - identity is the address only.
        raise SOL.ChainError("BAD_MINT", f"{mint!r} is not a mint address (32-44 base58 "
                             "chars). Tokens are never resolved by symbol.")
    try:
        raw = _b58_decode(mint)
    except ValueError as exc:
        raise SOL.ChainError("BAD_MINT", f"{mint!r} is not valid base58: {exc}") from None
    if len(raw) != 32:
        raise SOL.ChainError("BAD_MINT", f"{mint!r} decodes to {len(raw)} bytes, expected 32")
    if mint == input_mint:
        raise SOL.ChainError("BAD_MINT",
                             f"output mint equals input mint {input_mint} - nothing to buy")
    return mint


def to_atomic(amount: Any, decimals: int) -> int:
    """Human amount -> atomic units, truncating (never rounding up a spend)."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise SOL.ChainError("BAD_AMOUNT", f"{amount!r} is not a number") from None
    if value <= 0:
        raise SOL.ChainError("BAD_AMOUNT", f"amount must be > 0, got {value}")
    atomic = int(value.scaleb(int(decimals)).to_integral_value(rounding="ROUND_DOWN"))
    if atomic <= 0:
        raise SOL.ChainError("BAD_AMOUNT", f"{value} rounds to 0 atomic units at {decimals} decimals")
    return atomic


# ---------- private: order checks ----------

def _require(order: dict, field: str) -> Any:
    if field not in order or order[field] in (None, ""):
        raise SOL.ChainError("MISSING_FIELD",
                             f"order response has no usable {field!r} - refusing to proceed")
    return order[field]


def _assert_order_safe(order: dict, input_mint: str, output_mint: str,
                       max_price_impact_pct: Decimal, amount_atomic: int,
                       slippage_bps: int) -> None:
    """Fail-closed gate between quoting and signing."""
    if not isinstance(order, dict):
        raise SOL.ChainError("BAD_ORDER", f"expected an order object, got {type(order).__name__}")
    # simulationError must be PRESENT and null.  Absent == we cannot prove the
    # swap simulates, which is not the same as "it is fine".
    if "simulationError" not in order:
        raise SOL.ChainError("NO_SIMULATION", "order response omitted 'simulationError' - cannot "
                             "confirm the swap simulates; aborting before signing")
    sim = order["simulationError"]
    if sim:
        detail = sim if isinstance(sim, str) else json.dumps(sim, sort_keys=True)[:300]
        code = sim.get("errorCode", "SIMULATION_ERROR") if isinstance(sim, dict) \
            else "SIMULATION_ERROR"
        raise SOL.SwapReverted(code, f"simulation failed before signing: {detail}")
    # The response must describe the swap we actually asked for.
    if _require(order, "inputMint") != input_mint or _require(order, "outputMint") != output_mint:
        raise SOL.ChainError("MINT_MISMATCH",
                             f"order is for {order.get('inputMint')}->{order.get('outputMint')}, "
                             f"expected {input_mint}->{output_mint}")
    # ...and for the AMOUNT we asked for.  Jupiter echoes the request exactly on
    # an ExactIn swap (verified live: amount=1000000 -> inAmount "1000000"), so a
    # mismatch means we are about to sign a transaction that spends something
    # other than what the caller authorised - including an ExactOut order, where
    # `amount` is the OUTPUT and the input spend is bounded only by slippage.
    if int(_require(order, "inAmount")) != int(amount_atomic):
        raise SOL.ChainError("AMOUNT_MISMATCH",
                             f"order is for inAmount {order.get('inAmount')} atomic units, "
                             f"we asked to spend {int(amount_atomic)} - refusing to sign")
    out_amount = int(_require(order, "outAmount"))
    if out_amount <= 0:
        raise SOL.ChainError("ZERO_OUT", "order quotes a zero output amount")
    # The slippage the ROUTER will enforce, echoed back.  Only a value LARGER
    # than we asked for is a problem: it widens the worst-case fill past what
    # the caller authorised.  A tighter echo is Jupiter being conservative.
    echoed_bps = int(_require(order, "slippageBps"))
    if echoed_bps > int(slippage_bps):
        raise SOL.ChainError("SLIPPAGE_MISMATCH",
                             f"order carries slippageBps {echoed_bps}, we asked for "
                             f"{int(slippage_bps)} - the worst-case fill is not the one "
                             "that was authorised")
    # Minimum received, mirroring bsc_swap's amountOutMin: `outAmount` is a
    # quote, `otherAmountThreshold` is what the transaction actually enforces.
    threshold = int(_require(order, "otherAmountThreshold"))
    floor = out_amount * (10_000 - echoed_bps) // 10_000
    if threshold <= 0 or threshold < floor:
        raise SOL.ChainError("MIN_RECEIVED",
                             f"otherAmountThreshold {threshold} is below the "
                             f"{floor} implied by outAmount {out_amount} at "
                             f"{echoed_bps}bps - the order protects less than it quotes")
    impact = order.get("priceImpactPct")
    if impact is None:
        raise SOL.ChainError("NO_PRICE_IMPACT", "order response omitted 'priceImpactPct' - aborting")
    if abs(Decimal(str(impact))) * 100 > max_price_impact_pct:
        raise SOL.ChainError("PRICE_IMPACT", f"price impact {Decimal(str(impact)) * 100:.2f}% "
                             f"exceeds the {max_price_impact_pct}% ceiling")


def _route(order: dict) -> list:
    """Flatten routePlan into [{amm, percent, in_mint, out_mint}] for logging."""
    plan = []
    for hop in order.get("routePlan") or []:
        info = hop.get("swapInfo") or {}
        plan.append({"amm": info.get("label") or info.get("ammKey"),
                     "percent": hop.get("percent"),
                     "in_mint": info.get("inputMint"),
                     "out_mint": info.get("outputMint")})
    return plan


# ---------- private: signing ----------

def _keypair(key: SecretKey):
    """Build a solders Keypair from the wrapped secret.

    ``SecretKey.reveal()`` is called inline and its result is never bound to a
    name that outlives this frame, never logged, and never re-raised: the
    original exception is swallowed because solders echoes the input bytes.
    Accepts the two shapes people actually have on disk - a base58 secret key
    (``wallet_keys.load_secret_key(..., fmt="base58")``) and a solana-keygen
    JSON byte array.
    """
    if not isinstance(key, SecretKey):
        raise SOL.ChainError("KEY_TYPE", "expected a SecretKey, not raw key material")
    from solders.keypair import Keypair  # imported late: optional dependency

    text = None
    try:
        text = key.reveal().strip()
        if text.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(text)))
        return Keypair.from_base58_string(text)
    except Exception:
        # Swallowing the cause is not enough: `text` IS the raw secret and this
        # frame goes into the new exception's traceback, where any locals-
        # capturing handler (Sentry, rich, cgitb) would render it.  Drop the
        # reference before raising so the frame carries nothing.
        text = None
        raise SOL.ChainError("BAD_KEY", "wallet secret could not be parsed as a "
                                        "Solana keypair") from None


def taker_address(key: SecretKey) -> str:
    """Derive the wallet's base58 public key (the Jupiter ``taker``)."""
    return str(_keypair(key).pubkey())


def _resolve_key(key: Optional[SecretKey], key_env: str) -> SecretKey:
    """Lazily load the signing key so a dry run with an explicit ``taker``
    never touches the key file at all.  ``key_env`` names an env var holding a
    PATH (house rule: a private key is never read from a plain env var)."""
    return key if key is not None else load_secret_key(key_env, fmt="base58")


def _sign(tx_b64: str, keypair) -> str:
    """Reconstruct-and-sign: VersionedTransaction is immutable in solders.

    Guarded like ``_keypair``: solders renders a Keypair's raw bytes under
    ``repr``, and ``keypair`` is a local of this frame, so neither the original
    exception nor this frame may reach a traceback a log/APM handler renders.
    """
    from solders.transaction import VersionedTransaction

    try:
        raw_tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(raw_tx.message, [keypair])
        return base64.b64encode(bytes(signed)).decode("ascii")
    except Exception:
        keypair = None  # drop the key material out of this frame's locals
        raise SOL.ChainError("SIGN_FAILED", "the order transaction could not be "
                                            "reconstructed and signed") from None


# ---------- public ----------

def buy_token(client=None, key: Optional[SecretKey] = None, mint: Optional[str] = None,
              usdt_amount=None, slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
              dry_run: bool = True, taker: Optional[str] = None,
              input_mint: str = USDT_MINT,
              sol_floor_lamports: int = DEFAULT_SOL_FLOOR_LAMPORTS,
              max_price_impact_pct: Decimal = DEFAULT_MAX_PRICE_IMPACT_PCT,
              max_requotes: int = MAX_REQUOTES, *,
              token_mint: Optional[str] = None, rpc_url: Optional[str] = None,
              api_key: Optional[str] = None, min_native_balance=None,
              key_env: str = KEY_ENV) -> Dict[str, Any]:
    """Swap `usdt_amount` USDT for `mint` via Jupiter.  Dry-run unless told otherwise.

    Order of operations: mint sanity (free, so first) -> SOL floor -> order ->
    simulationError -> (dry_run returns the plan HERE, before any signing) ->
    reconstruct-and-sign -> execute.  A stale blockhash re-quotes up to
    `max_requotes` times, then gives up; a stale signed tx is never resubmitted.

    `key` is only ever touched to derive `taker` and to sign; pass `taker`
    explicitly and a dry run never opens the key file at all.

    Keyword aliases exist for memebuy.orchestrator, which calls
    ``buy_token(usdt_amount=, slippage_bps=, rpc_url=, min_native_balance=,
    token_mint=, dry_run=)`` and holds neither a client nor key material: the
    client is then built here and the key is read from the file named by
    ``key_env`` (an env var holding a PATH).
    """
    mint = mint or token_mint
    # `if dry_run:` alone reads None/0/"" as LIVE.  A money mover never guesses:
    # the flag has to be an actual bool or nothing is sent.
    if not isinstance(dry_run, bool):
        raise SOL.ChainError("BAD_DRY_RUN",
                             f"dry_run must be a bool, got {type(dry_run).__name__} "
                             f"{dry_run!r} - refusing to guess whether this is a live trade")
    if usdt_amount is None:
        raise SOL.ChainError("BAD_AMOUNT", "no usdt_amount given")
    validate_mint(mint, input_mint=input_mint)
    if int(slippage_bps) <= 0 or int(slippage_bps) > 5000:
        raise SOL.ChainError("BAD_SLIPPAGE",
                             f"slippage_bps {slippage_bps} outside the sane 1-5000 range")
    if client is None:
        client = SolanaClient(rpc_url=rpc_url or SOLANA_RPC_URL, api_key=api_key)
    # Config gives SOL, we work in lamports.  A non-positive setting means
    # "unconfigured", NOT "no floor": memebuy.config defaults min_native_balance
    # to 0.0, and honouring that would silently drop the floor from 0.04 SOL to
    # MIN_SANE_FLOOR_LAMPORTS on every default deployment.
    if min_native_balance is not None:
        configured = int(Decimal(str(min_native_balance)) * LAMPORTS_PER_SOL)
        if configured > 0:
            sol_floor_lamports = configured
    sol_floor_lamports = max(int(sol_floor_lamports), MIN_SANE_FLOOR_LAMPORTS)

    if taker is None:
        taker = taker_address(_resolve_key(key, key_env))

    # --- SOL floor: fees + 0.00203928 SOL of ATA rent for a first-ever buy ---
    lamports = client.get_sol_balance(taker)
    if lamports < int(sol_floor_lamports):
        raise SOL.InsufficientGas(
            "SOL_FLOOR",
            f"SOL balance {lamports / LAMPORTS_PER_SOL:.6f} is below the "
            f"{int(sol_floor_lamports) / LAMPORTS_PER_SOL:.6f} floor "
            f"(ATA rent alone is {ATA_RENT_LAMPORTS / LAMPORTS_PER_SOL:.8f} SOL)")

    # --- atomic conversion from the mint's own decimals, not a constant ---
    decimals = int(client.get_mint_info(input_mint)["decimals"])
    amount_atomic = to_atomic(usdt_amount, decimals)

    attempt = 0
    while True:
        order = client.get_order(input_mint=input_mint, output_mint=mint,
                                 amount_atomic=amount_atomic, slippage_bps=int(slippage_bps),
                                 taker=taker, restrict_intermediate_tokens=True,
                                 dynamic_compute_unit_limit=True)
        _assert_order_safe(order, input_mint, mint, max_price_impact_pct,
                           amount_atomic, int(slippage_bps))

        result = {
            "dry_run": bool(dry_run),
            "signature": None,
            "in_amount": int(_require(order, "inAmount")),
            "out_amount": int(order["outAmount"]),
            "price_impact": str(order["priceImpactPct"]),
            "route": _route(order),
            "input_mint": input_mint,
            "output_mint": mint,
            "slippage_bps": int(slippage_bps),
            "taker": taker,
        }

        if dry_run:
            LOG.info("DRY-RUN buy: GET %s/swap/v2/order inputMint=%s outputMint=%s amount=%s "
                     "slippageBps=%s taker=%s restrictIntermediateTokens=true -> out=%s impact=%s "
                     "route=%s ; POST /swap/v2/execute WOULD FOLLOW (not sent)",
                     getattr(client, "base_url", "?"), input_mint, mint, amount_atomic,
                     slippage_bps, taker, result["out_amount"], result["price_impact"],
                     result["route"])
            result["intended_call"] = {
                "endpoint": f"{getattr(client, 'base_url', '')}/swap/v2/order",
                "params": {"inputMint": input_mint, "outputMint": mint,
                           "amount": str(amount_atomic), "taker": taker,
                           "slippageBps": str(int(slippage_bps)),
                           "restrictIntermediateTokens": "true"},
            }
            return result

        signed_tx = _sign(_require(order, "transaction"),
                          _keypair(_resolve_key(key, key_env)))
        try:
            executed = client.execute_order(signed_tx, _require(order, "requestId"))
        except QuoteStale as exc:
            attempt += 1
            if attempt > int(max_requotes):
                raise SOL.QuoteStale("STALE_GIVEUP", f"blockhash went stale {attempt}x; giving "
                                     f"up after {max_requotes} re-quotes") from exc
            LOG.warning("blockhash stale (%s); re-quoting %d/%d", exc, attempt, max_requotes)
            continue  # RE-QUOTE - never resubmit a stale signed transaction
        except SwapReverted:
            raise

        result["signature"] = executed["signature"]
        result["slot"] = executed.get("slot")
        # Jupiter reports the settled amounts; prefer them over the quote.
        result["in_amount"] = int(executed.get("inputAmountResult") or result["in_amount"])
        result["out_amount"] = int(executed.get("outputAmountResult") or result["out_amount"])
        LOG.info("buy filled: %s in=%s out=%s impact=%s", result["signature"],
                 result["in_amount"], result["out_amount"], result["price_impact"])
        return result
