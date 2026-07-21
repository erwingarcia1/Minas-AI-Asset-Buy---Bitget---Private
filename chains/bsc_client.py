"""BNB Smart Chain RPC client: connection + read-only helpers.

Docs: https://web3py.readthedocs.io/en/v7.16.0/  (web3.py 7.16.x)
      https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/

web3.py v7 notes that bite when porting v6 code:
  * the PoA middleware is ``ExtraDataToPOAMiddleware`` (v6: ``geth_poa_middleware``)
    and must be injected at layer 0 - BSC packs validator data into
    ``extraData``, so without it every ``eth_getBlock`` raises.
  * a signed tx exposes ``.raw_transaction`` (v6: ``.rawTransaction``).

The RPC URL is configurable (``BSC_RPC_URL``) on purpose: the public
``bsc-dataseed*.bnbchain.org`` nodes are aggressively rate-limited and have
``eth_getLogs`` DISABLED, so never build event-watching on the default.

``web3`` is imported lazily inside :func:`connect` so this module (and the
swap module that imports it) stays importable - and unit-testable - on a
host without the web3 stack.
"""

import logging
import os

from .errors import chain_error_family

LOG = logging.getLogger(__name__)

# Chain-labelled errors ("bsc error WRONG_CHAIN: ..."). Built once here and
# imported by bsc_swap; still catchable as the shared ChainError family.
BSC = chain_error_family("bsc")

BSC_CHAIN_ID = 56
DEFAULT_RPC_URL = "https://bsc-dataseed.bnbchain.org"
DEFAULT_TIMEOUT = 20  # seconds

# Gas on BSC sits around 0.05-1 gwei. Clamp both ends: a node returning 0
# gets us stuck in the mempool, a node returning 50 gwei burns real money.
DEFAULT_GAS_FLOOR_GWEI = 0.1
DEFAULT_GAS_CEIL_GWEI = 5.0

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

_DECIMALS_CACHE = {}  # token address -> int; decimals() is immutable in practice


# ---------- public ----------

def connect(rpc_url=None, timeout=None, expected_chain_id=BSC_CHAIN_ID):
    """Build a Web3 bound to BSC, with the PoA middleware and a chain-id assert."""
    rpc_url = rpc_url or os.environ.get("BSC_RPC_URL") or DEFAULT_RPC_URL
    timeout = timeout or float(os.environ.get("BSC_RPC_TIMEOUT", DEFAULT_TIMEOUT))
    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
    except ImportError as exc:  # pragma: no cover - depends on host env
        raise BSC.ChainError("DEP_MISSING", f"web3>=7 is required: {exc}")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    # Layer 0 = outermost: the extraData fix has to run before anything else
    # touches a block, including the chain-id probe below.
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    try:
        chain_id = w3.eth.chain_id
    except Exception as exc:
        raise BSC.ChainError("RPC_UNREACHABLE", f"{rpc_url}: {type(exc).__name__}: {exc}")
    if chain_id != expected_chain_id:
        raise BSC.ChainError(
            "WRONG_CHAIN",
            f"{rpc_url} reports chain id {chain_id}, expected {expected_chain_id}",
        )
    LOG.info("connected to BSC via %s (chain id %s)", rpc_url, chain_id)
    return w3


def native_balance(w3, address):
    """BNB balance of ``address`` in wei. Gas is paid in BNB, not USDT.

    A node failure (429 from a public dataseed, timeout) is raised as a typed
    ChainError like every other read here, so ``buy_token``'s documented "any
    failure is a REJECT" holds for the caller's ``except ChainError`` too.
    """
    try:
        return int(w3.eth.get_balance(w3.to_checksum_address(address)))
    except Exception as exc:
        raise BSC.ChainError("BALANCE_READ", f"eth_getBalance failed: {exc}")


def token_balance(w3, token_address, owner):
    """BEP-20 balance in the token's own base units."""
    try:
        return int(erc20(w3, token_address).functions.balanceOf(
            w3.to_checksum_address(owner)).call())
    except Exception as exc:
        raise BSC.ChainError(
            "BALANCE_READ", f"balanceOf failed for {token_address}: {exc}")


def token_decimals(w3, token_address, use_cache=True):
    """Read ``decimals()`` at runtime. NEVER hardcode.

    BSC USDT (0x55d3...7955) has **18** decimals, not the 6 it has on
    Ethereum/Tron - assuming 6 is the single most common porting bug and
    silently spends 1e12x the intended amount.
    """
    token = w3.to_checksum_address(token_address)
    if use_cache and token in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[token]
    try:
        value = int(erc20(w3, token).functions.decimals().call())
    except Exception as exc:
        raise BSC.ChainError("DECIMALS_READ", f"decimals() failed for {token}: {exc}")
    if not 0 <= value <= 36:
        raise BSC.ChainError("DECIMALS_INSANE", f"{token} reports decimals={value}")
    _DECIMALS_CACHE[token] = value
    return value


def token_symbol(w3, token_address):
    """Best-effort symbol, for log lines only. Never used for decisions."""
    try:
        return str(erc20(w3, token_address).functions.symbol().call())
    except Exception:
        return "?"


def gas_price(w3, floor_gwei=DEFAULT_GAS_FLOOR_GWEI, ceil_gwei=DEFAULT_GAS_CEIL_GWEI):
    """Node gas price in wei, clamped into [floor, ceiling]."""
    try:
        raw = int(w3.eth.gas_price)
    except Exception as exc:
        raise BSC.ChainError("GAS_PRICE_READ", f"eth_gasPrice failed: {exc}")
    floor = int(floor_gwei * 1e9)
    ceiling = int(ceil_gwei * 1e9)
    if raw < floor:
        LOG.warning("node gas price %s wei below floor, clamping to %s", raw, floor)
        return floor
    if raw > ceiling:
        LOG.warning("node gas price %s wei above ceiling, clamping to %s", raw, ceiling)
        return ceiling
    return raw


def erc20(w3, token_address):
    """Minimal BEP-20 contract handle."""
    return w3.eth.contract(address=w3.to_checksum_address(token_address),
                           abi=ERC20_ABI)


def is_zero_address(address):
    """True for None / 0x000...0. Anything unparseable is a typed REJECT.

    It is fed factory ``getPair`` output, i.e. external data - returning False
    for a value we cannot read would quietly promote garbage to "a real pair".
    """
    if address is None:
        return True
    try:
        return int(str(address), 16) == 0
    except ValueError:
        raise BSC.ChainError("ADDRESS_MALFORMED", f"not a hex address: {address!r}")


def to_base_units(amount, decimals):
    """Human amount -> integer base units, truncating (never rounding up).

    ``amount`` is accepted as str/int/Decimal; float is tolerated but goes
    through ``str()`` first so 0.1 does not become 0.09999999999999999.

    The scaling runs in a WIDE local context on purpose: at the default 28-digit
    precision ``Decimal(x) * 10**18`` silently rounds - and rounds half-UP, so
    ``"99999999999.999999999999999999"`` came out as 1e29, one base unit MORE
    than asked. Spending more than the caller said is never acceptable here.
    """
    from decimal import Decimal, InvalidOperation, localcontext
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise BSC.ChainError("AMOUNT_INVALID", f"cannot parse amount {amount!r}")
    # is_finite() first: NaN/Infinity would make the comparison below raise a
    # bare InvalidOperation, or (Infinity) sail past it as "positive".
    if not value.is_finite() or value <= 0:
        raise BSC.ChainError("AMOUNT_INVALID", f"amount must be positive, got {amount!r}")
    with localcontext() as ctx:
        ctx.prec = 80
        return int(value * (Decimal(10) ** decimals))


def from_base_units(units, decimals):
    """Integer base units -> Decimal human amount (display/logging only)."""
    from decimal import Decimal
    return Decimal(int(units)) / (Decimal(10) ** decimals)
