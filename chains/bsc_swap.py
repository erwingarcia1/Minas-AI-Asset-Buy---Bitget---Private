"""Buy a BEP-20 token with USDT on PancakeSwap V2 (BNB Smart Chain).

Docs: https://docs.pancakeswap.finance/developers/smart-contracts/pancakeswap-exchange/v2-contracts/router-v2
      https://web3py.readthedocs.io/en/v7.16.0/

Hard-won rules baked into this module:
  * ALWAYS ``swapExactTokensForTokensSupportingFeeOnTransferTokens``. Plain
    ``swapExactTokensForTokens`` reverts with "Pancake: K" on any token with a
    transfer tax, and the SupportingFeeOnTransfer variant is equally correct
    for zero-tax tokens. There is no branch here on purpose.
  * BSC USDT has **18** decimals. Decimals are read at runtime anyway.
  * ``getAmountsOut`` models the 0.25% LP fee ONLY - it knows nothing about a
    token's own buy tax. Budget that separately via ``buy_tax_bps`` or you get
    INSUFFICIENT_OUTPUT_AMOUNT every single time.
  * Approve the EXACT amount for this trade. Never infinite.
  * Gas is paid in BNB: a USDT-only wallet cannot even send the approve, so we
    hard-fail below a BNB floor before doing anything else.
  * ``estimate_gas`` pre-flight: if the node says it reverts, we ABORT and log
    the reason. We never broadcast a tx we already know will fail.

DRY-RUN IS THE DEFAULT. ``dry_run=True`` still does the full read-only plan
(balances, decimals, route, quote, allowance) so the log shows a realistic
trade, then stops before ANY state-changing call.
"""

import logging
import time

from . import bsc_client as bc
from .bsc_client import BSC  # chain-labelled ChainError/SwapReverted/... family
from .wallet_keys import SecretKey, address_from_key

LOG = logging.getLogger(__name__)

PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
USDT = "0x55d398326f99059fF775485246999027B3197955"   # 18 decimals on BSC
WBNB = "0xbb4CdB9CbD36B01bD1cBaEBF2De08d9173bc095c"

# ~0.003 BNB covers an approve + a swap with headroom at BSC gas prices.
DEFAULT_MIN_BNB_WEI = 3 * 10 ** 15
DEFAULT_DEADLINE_SECONDS = 120
DEFAULT_GAS_MULTIPLIER = 1.25   # fee-on-transfer swaps overshoot the estimate
MAX_SLIPPAGE_BPS = 5000         # 50%; anything above is a fat-finger

ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForTokensSupportingFeeOnTransferTokens",
     "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": []},
]

FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"}],
     "outputs": [{"name": "pair", "type": "address"}]},
]


# ---------- public ----------

def buy_token(w3, key, token_address, usdt_amount, slippage_bps, recipient,
              dry_run=True, buy_tax_bps=0, min_bnb_wei=DEFAULT_MIN_BNB_WEI,
              deadline_seconds=DEFAULT_DEADLINE_SECONDS,
              gas_multiplier=DEFAULT_GAS_MULTIPLIER, receipt_timeout=180):
    """Swap ``usdt_amount`` USDT for ``token_address`` on PancakeSwap V2.

    ``usdt_amount`` is a HUMAN amount ("25", Decimal("25.5")); it is converted
    with USDT's runtime ``decimals()``. ``slippage_bps`` covers price impact +
    drift; ``buy_tax_bps`` covers the token's own transfer tax - they are
    added together to compute ``amountOutMin``.

    Returns ``{tx_hash, amount_in, amount_out_min, expected_out, path,
    gas_used, status}`` (``status`` is ``"success"`` or ``"dry_run"``). Every
    failure is a ChainError and a REJECT; a partial result is never returned.

    CALLER CONTRACT - two error codes are NOT safe to retry, because the money
    may already be in flight and a retry buys twice::

        ChainError.code == "BROADCAST_UNCONFIRMED"  # exc.nonce set
        ChainError.code == "RECEIPT_UNCONFIRMED"    # exc.tx_hash set

    Reconcile that hash/nonce on chain before re-calling. Everything else
    (InsufficientGas, QuoteStale, SwapReverted, the validation codes) is raised
    before anything was broadcast and is safe to retry.
    """
    if not isinstance(key, SecretKey):
        raise BSC.ChainError("KEY_TYPE", "key must be a SecretKey (see wallet_keys)")
    # bps must be whole numbers: a float turns amountOutMin into a float, which
    # is both inexact past 2**53 and not a uint256 the ABI encoder will take -
    # and it would only blow up AFTER the approve is already on chain.
    if isinstance(slippage_bps, bool) or isinstance(buy_tax_bps, bool) \
            or not isinstance(slippage_bps, int) or not isinstance(buy_tax_bps, int):
        raise BSC.ChainError(
            "SLIPPAGE_TYPE",
            f"slippage_bps and buy_tax_bps must be whole ints, got "
            f"{type(slippage_bps).__name__}/{type(buy_tax_bps).__name__}")
    # They are ADDED to form the real floor, so the fat-finger cap has to apply
    # to the SUM: 4000 + 4000 individually "in range" would accept 20% of quote.
    effective_bps = slippage_bps + buy_tax_bps
    if not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS \
            or not 0 <= buy_tax_bps <= MAX_SLIPPAGE_BPS \
            or effective_bps > MAX_SLIPPAGE_BPS:
        raise BSC.ChainError(
            "SLIPPAGE_RANGE",
            f"slippage_bps={slippage_bps} + buy_tax_bps={buy_tax_bps} "
            f"= {effective_bps}; each of them AND their sum must be "
            f"within 0..{MAX_SLIPPAGE_BPS}")

    sender = address_from_key(key)
    recipient = w3.to_checksum_address(recipient)
    token = w3.to_checksum_address(token_address)
    usdt = w3.to_checksum_address(USDT)
    router_address = w3.to_checksum_address(PANCAKE_V2_ROUTER)
    if bc.is_zero_address(recipient) or bc.is_zero_address(token):
        raise BSC.ChainError("ADDRESS_ZERO", "recipient and token must be non-zero")
    if token == usdt:
        raise BSC.ChainError("SAME_TOKEN", "cannot swap USDT for USDT")

    # 1. BNB floor - gas is paid in BNB; fail closed before anything else.
    bnb = bc.native_balance(w3, sender)
    if bnb < min_bnb_wei:
        raise BSC.InsufficientGas(
            "BNB_FLOOR",
            f"{bc.from_base_units(bnb, 18)} BNB is below the "
            f"{bc.from_base_units(min_bnb_wei, 18)} BNB floor; a USDT-only "
            f"wallet cannot pay for the approve or the swap",
        )

    # 2. Decimals at runtime. BSC USDT is 18, NOT 6.
    usdt_decimals = bc.token_decimals(w3, usdt)
    amount_in = bc.to_base_units(usdt_amount, usdt_decimals)

    usdt_held = bc.token_balance(w3, usdt, sender)
    if usdt_held < amount_in:
        raise BSC.ChainError(
            "USDT_BALANCE",
            f"have {bc.from_base_units(usdt_held, usdt_decimals)} USDT, "
            f"need {usdt_amount}")

    # 3-4. Route discovery + quote.
    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    path, expected_out = _best_path(w3, router, usdt, token, amount_in)

    # 5. amountOutMin. getAmountsOut knows the LP fee only, so the token's own
    #    buy tax has to be budgeted here or the swap reverts on arrival.
    #    (``effective_bps`` was range-checked as a whole int up top.)
    amount_out_min = expected_out * (10_000 - effective_bps) // 10_000
    if amount_out_min <= 0:
        raise BSC.QuoteStale(
            "QUOTE_ZERO",
            f"amountOutMin computed as 0 from expected_out={expected_out}")

    plan = {
        "sender": sender, "recipient": recipient, "token": token,
        "amount_in": amount_in, "expected_out": expected_out,
        "amount_out_min": amount_out_min, "path": path,
        "slippage_bps": slippage_bps, "buy_tax_bps": buy_tax_bps,
    }
    LOG.info("PLAN pancakeV2 swapExactTokensForTokensSupportingFeeOnTransferTokens "
             "amountIn=%s (%s USDT, %s decimals) amountOutMin=%s expectedOut=%s "
             "path=%s to=%s slippage=%sbps buyTax=%sbps dry_run=%s",
             amount_in, usdt_amount, usdt_decimals, amount_out_min, expected_out,
             " -> ".join(path), recipient, slippage_bps, buy_tax_bps, dry_run)

    # 6. Allowance - exact approve per trade, never infinite.
    allowance = int(bc.erc20(w3, usdt).functions.allowance(sender, router_address).call())
    needs_approve = allowance < amount_in

    if dry_run:
        # Read-only pre-flight only when it can actually succeed: without the
        # allowance in place estimate_gas would revert TRANSFER_FROM_FAILED and
        # tell us nothing about the swap itself.
        gas_estimate = None
        if needs_approve:
            LOG.info("DRY RUN would approve EXACTLY %s USDT base units to %s "
                     "(allowance %s); skipping gas pre-flight until it exists",
                     amount_in, router_address, allowance)
        else:
            gas_estimate = _preflight(_swap_fn(router, plan, deadline_seconds), sender)
        LOG.info("DRY RUN - nothing sent. %s", plan)
        return _result(None, plan, gas_estimate, "dry_run")

    if needs_approve:
        _approve_exact(w3, key, sender, usdt, router_address, amount_in, receipt_timeout)

    # 7. estimate_gas PRE-FLIGHT. A revert here aborts: we never broadcast a
    #    transaction we already know will fail.
    swap_fn = _swap_fn(router, plan, deadline_seconds)
    gas_estimate = _preflight(swap_fn, sender)

    # 8-9. Sign locally, send, wait.
    tx = swap_fn.build_transaction({
        "from": sender,
        "nonce": w3.eth.get_transaction_count(sender),
        "gasPrice": bc.gas_price(w3),
        "gas": int(gas_estimate * gas_multiplier),
        "chainId": bc.BSC_CHAIN_ID,
    })
    tx_hash = _sign_and_send(w3, key, tx)
    LOG.info("swap broadcast tx=%s", tx_hash)
    receipt = _await_receipt(w3, tx_hash, receipt_timeout, "swap")

    if not _status_ok(receipt):
        raise BSC.SwapReverted("SWAP_FAILED", f"swap tx {tx_hash} mined with status 0")
    return _result(tx_hash, plan, _field(receipt, "gasUsed"), "success")


# ---------- internal ----------

def _best_path(w3, router, usdt, token, amount_in):
    """Direct USDT->TOKEN if the pair exists, else route via WBNB.

    When both exist we quote both and take the better output - a thin direct
    pair often prices worse than the deep USDT/WBNB + WBNB/TOKEN route.
    """
    wbnb = w3.to_checksum_address(WBNB)
    factory = w3.eth.contract(address=w3.to_checksum_address(PANCAKE_V2_FACTORY),
                              abi=FACTORY_ABI)
    try:
        pair = factory.functions.getPair(usdt, token).call()
    except Exception as exc:
        raise BSC.QuoteStale("FACTORY_READ", f"getPair failed: {exc}")

    # Buying WBNB itself is a single hop; [USDT, WBNB, WBNB] is not a path.
    candidates = [[usdt, wbnb]] if token == wbnb else [[usdt, wbnb, token]]
    if token != wbnb and not bc.is_zero_address(pair):
        candidates.insert(0, [usdt, token])
    else:
        LOG.info("no direct USDT/%s pair; routing through WBNB", token)

    best_path, best_out = None, 0
    for candidate in candidates:
        try:
            amounts = router.functions.getAmountsOut(amount_in, candidate).call()
            out = int(amounts[-1])
        except Exception as exc:
            LOG.info("path %s unusable: %s", " -> ".join(candidate), exc)
            continue
        if out > best_out:
            best_path, best_out = candidate, out

    if not best_path or best_out <= 0:
        raise BSC.QuoteStale(
            "NO_ROUTE",
            f"no PancakeSwap V2 route from USDT to {token} with a non-zero quote")
    return best_path, best_out


def _swap_fn(router, plan, deadline_seconds):
    return router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
        plan["amount_in"], plan["amount_out_min"], plan["path"],
        plan["recipient"], int(time.time()) + deadline_seconds)


def _preflight(fn, sender):
    """eth_estimateGas as a revert check. Raises SwapReverted with the reason."""
    try:
        return int(fn.estimate_gas({"from": sender}))
    except Exception as exc:
        reason = _revert_reason(exc)
        LOG.error("ABORT - gas pre-flight reverted: %s", reason)
        raise BSC.SwapReverted("PREFLIGHT_REVERT", reason)


def _approve_exact(w3, key, sender, usdt, spender, amount_in, receipt_timeout):
    """Approve EXACTLY this trade's amount. Never infinite."""
    fn = bc.erc20(w3, usdt).functions.approve(spender, amount_in)
    gas = _preflight(fn, sender)
    tx = fn.build_transaction({
        "from": sender,
        "nonce": w3.eth.get_transaction_count(sender),
        "gasPrice": bc.gas_price(w3),
        "gas": int(gas * 1.2),
        "chainId": bc.BSC_CHAIN_ID,
    })
    tx_hash = _sign_and_send(w3, key, tx)
    LOG.info("approve %s USDT base units to %s tx=%s", amount_in, spender, tx_hash)
    receipt = _await_receipt(w3, tx_hash, receipt_timeout, "approve")
    if not _status_ok(receipt):
        # An unmined/failed approve is what TRANSFER_FROM_FAILED looks like later.
        raise BSC.SwapReverted("APPROVE_FAILED", f"approve tx {tx_hash} mined with status 0")
    return tx_hash


def _sign_and_send(w3, key, tx):
    """Sign locally and broadcast. web3 v7: ``signed.raw_transaction``."""
    # This is the ONE place raw key material exists. Anything the signer raises
    # is re-raised as the exception TYPE only - same discipline as
    # wallet_keys.address_from_key - because a signer that echoes its input
    # would otherwise put the private key into a log line and a traceback.
    try:
        signed = w3.eth.account.sign_transaction(tx, private_key=key.reveal())
    except Exception as exc:
        # "from None" is load-bearing, not style: without it Python chains the
        # original exception and traceback.format_exc() reprints its message -
        # key and all - under "During handling of the above exception".
        raise BSC.ChainError(
            "SIGN_FAILED",
            f"could not sign the transaction ({type(exc).__name__}); "
            f"the reason is withheld because the signer was handed key material"
        ) from None
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raise BSC.ChainError(
            "WEB3_VERSION",
            "signed tx has no .raw_transaction - web3.py >= 7 required")
    try:
        return w3.to_hex(w3.eth.send_raw_transaction(raw))
    except Exception as exc:
        # The node may already hold the tx and only the RESPONSE was lost, so
        # this is NOT a safe-to-retry failure: re-sending on a fresh nonce would
        # buy twice. Surface it as unconfirmed and make the caller reconcile.
        raise _unconfirmed(
            "BROADCAST_UNCONFIRMED",
            f"send_raw_transaction failed after signing "
            f"({type(exc).__name__}: {exc}); the node may already hold this tx - "
            f"reconcile nonce {_field(tx, 'nonce')} for the sender on chain "
            f"before ANY retry",
            nonce=_field(tx, "nonce"))


def _await_receipt(w3, tx_hash, timeout, what):
    """Wait for a mined receipt, or raise an UNCONFIRMED error carrying the hash.

    A timeout here is not a failed trade: the transaction is already broadcast
    and may still mine. Losing the hash in a bare ``TimeExhausted`` is how a
    caller ends up retrying and spending twice, so the hash travels with the
    exception (``exc.tx_hash``) and the code says "unconfirmed", not "failed".
    """
    try:
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception as exc:
        raise _unconfirmed(
            "RECEIPT_UNCONFIRMED",
            f"{what} tx {tx_hash} was broadcast but no receipt arrived within "
            f"{timeout}s ({type(exc).__name__}: {exc}); it may still mine - "
            f"check this hash on chain before ANY retry",
            tx_hash=tx_hash)


def _unconfirmed(code, message, tx_hash=None, nonce=None):
    """ChainError meaning "money may already be in flight" - never retry blind."""
    exc = BSC.ChainError(code, message)
    exc.tx_hash = tx_hash
    exc.nonce = nonce
    return exc


def _revert_reason(exc):
    """Pull a human reason out of a web3 error. Common ones on Pancake V2:

    * ``PancakeRouter: INSUFFICIENT_OUTPUT_AMOUNT`` - amountOutMin too tight,
      usually an unbudgeted buy tax.
    * ``TransferHelper: TRANSFER_FROM_FAILED`` - approve not mined yet, or the
      amount was computed with the wrong decimals.
    * ``Pancake: K`` - a fee-on-transfer token hit a non-supporting swap.
    """
    text = str(exc).strip()
    return text or type(exc).__name__


def _result(tx_hash, plan, gas_used, status):
    return {
        "tx_hash": tx_hash,
        "amount_in": plan["amount_in"],
        "amount_out_min": plan["amount_out_min"],
        "expected_out": plan["expected_out"],
        "path": plan["path"],
        # _as_int, not int(): a "0x30d40" gasUsed from a provider without result
        # formatters would raise here - AFTER the swap mined - and lose the hash.
        "gas_used": _as_int(gas_used),
        "status": status,
    }


def _field(receipt, name):
    """Receipts are AttributeDicts in web3, plain dicts in tests."""
    try:
        return receipt[name]
    except (TypeError, KeyError):
        return getattr(receipt, name, None)


def _status_ok(receipt):
    """True only when the receipt positively says ``status == 1``.

    Normalises the shapes a provider without result formatters can hand back
    (``"0x1"``, ``b"\\x01"``). Reading a SUCCESSFUL receipt as a revert is not a
    harmless false alarm: the caller sees a failed buy that already spent the
    USDT and retries it. Anything missing or unrecognised is still a FAILURE.
    """
    status = _as_int(_field(receipt, "status"))
    return status == 1


def _as_int(value):
    """int / hex-str / bytes -> int; None or anything unparseable -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None
