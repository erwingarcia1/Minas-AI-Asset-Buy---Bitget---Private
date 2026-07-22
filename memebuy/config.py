"""Parse and validate the ``meme_buy`` config block.

Validation is strict and total: unknown keys are rejected (a typo'd
``per_trade_usdt_capp`` must not silently fall back to a default), types are
checked, and anything that could turn into an unintended live spend is a hard
error at load time rather than a surprise at 3am.

Defaults are the safe ones: ``armed=false``, ``dry_run=true``.

THE ARMED FLAG IS NOT HOT-RELOADABLE.  Two independent inputs must agree:
  1. ``meme_buy.armed: true`` in the config file, and
  2. the process environment variable ``MEME_BUY_ARMED=1``.
The environment variable can only change by restarting the process, so a
hot-reload of the config file — or anything that can write that file, including
the webapp — cannot arm a running bot.  Arming stays a deliberate operator act
with a restart and a log line attached to it.  See orchestrator._armed, which
snapshots the resolved value once at construction and never re-reads it.

Chain / address format references:
  BSC (BEP20) addresses:   https://ethereum.org/en/developers/docs/accounts/
  Solana account keys:     https://solana.com/docs/core/accounts
  Bitget Wallet SwapX:     https://web3.bitget.com/en/docs/swapx/quick-start
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from . import caps
from .credentials import DEFAULT_MAIN_PREFIX, DEFAULT_SUB_PREFIX

ARM_ENV_VAR = "MEME_BUY_ARMED"

# Which router executes the swap once the USDT has landed in our own wallet.
#   native — PancakeSwap V2 on BSC / Jupiter on Solana (chains/{bsc,solana}_swap)
#   bitget — Bitget Wallet SwapX aggregator            (chains/bitget_swap)
SWAP_BACKEND_NATIVE = "native"
SWAP_BACKEND_BITGET = "bitget"
SWAP_BACKENDS = (SWAP_BACKEND_NATIVE, SWAP_BACKEND_BITGET)

# Env-var prefixes name environment variables, so they must look like one.
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_TOP_KEYS = {
    "armed",
    "dry_run",
    "per_trade_usdt_cap",
    "daily_usdt_cap",
    "kill_switch_file",
    "db_path",
    "arrival_timeout_s",
    "arrival_poll_interval_s",
    "bitget_sub_prefix",
    "bitget_main_prefix",
    "chains",
}
_CHAIN_KEYS = {
    "enabled",
    "withdraw_address",
    "withdraw_allowlist",
    "rpc_url",
    "min_native_balance",
    "slippage_bps",
    "swap_backend",
    "gasless",
    "token_allowlist",
}


class ConfigError(ValueError):
    """The meme_buy config block is invalid.  Never run with one of these."""


@dataclass(frozen=True)
class ChainConfig:
    name: str
    enabled: bool = False
    withdraw_address: str = ""
    withdraw_allowlist: Tuple[str, ...] = ()
    rpc_url: str = ""
    min_native_balance: float = 0.0
    slippage_bps: int = 100
    swap_backend: str = SWAP_BACKEND_NATIVE
    gasless: bool = False
    token_allowlist: Tuple[str, ...] = ()


@dataclass(frozen=True)
class MemeBuyConfig:
    armed: bool = False
    dry_run: bool = True
    per_trade_usdt_cap: float = 0.0
    daily_usdt_cap: float = 0.0
    kill_switch_file: str = ""
    db_path: str = ""
    arrival_timeout_s: float = 900.0
    arrival_poll_interval_s: float = 15.0
    # Env-var prefixes for the two DIFFERENT Bitget API keys.  They are not
    # interchangeable: the sub key cannot withdraw, the main key must not be the
    # transfer source.  See memebuy/credentials.py.
    bitget_sub_prefix: str = DEFAULT_SUB_PREFIX
    bitget_main_prefix: str = DEFAULT_MAIN_PREFIX
    chains: Mapping[str, ChainConfig] = field(default_factory=dict)

    def chain(self, name: str) -> ChainConfig:
        cc = self.chains.get(str(name).strip().lower())
        if cc is None or not cc.enabled:
            raise ConfigError("chain %r is not enabled" % (name,))
        return cc


# ---------- internal validators ----------


def _reject_unknown(block: Mapping[str, Any], allowed: set, where: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ConfigError("%s: unknown key(s): %s" % (where, ", ".join(unknown)))


def _req_bool(block: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = block.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError("%s.%s must be a boolean, got %r" % (where, key, value))
    return value


def _req_positive(block: Mapping[str, Any], key: str, where: str) -> float:
    value = block.get(key)
    number = caps._as_amount(value)
    if number is None or number <= 0:
        raise ConfigError("%s.%s must be a number > 0, got %r" % (where, key, value))
    return number


def _req_str(block: Mapping[str, Any], key: str, where: str) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s.%s must be a non-empty string" % (where, key))
    return value.strip()


def _env_prefix(block: Mapping[str, Any], key: str, default: str) -> str:
    """An env-var prefix, e.g. BITGET_MAIN -> BITGET_MAIN_API_KEY_FILE."""
    value = block.get(key, default)
    if not isinstance(value, str) or not _PREFIX_RE.match(value.strip()):
        raise ConfigError(
            "meme_buy.%s must be an environment-variable prefix matching "
            "[A-Z][A-Z0-9_]*, got %r" % (key, value)
        )
    return value.strip()


def _parse_backend(raw: Mapping[str, Any], where: str) -> Tuple[str, bool]:
    """(swap_backend, gasless).  An unknown backend is a hard config error.

    ``gasless`` only exists in the Bitget SwapX flow (the aggregator sponsors
    gas).  Accepting gasless=true under the native routers and quietly ignoring
    it would leave an operator believing a wallet needs no BNB/SOL — and the
    swap then fails, after the irreversible withdrawal.  So it is rejected.
    """
    backend_raw = raw.get("swap_backend", SWAP_BACKEND_NATIVE)
    if not isinstance(backend_raw, str):
        raise ConfigError("%s.swap_backend must be a string" % where)
    backend = backend_raw.strip().lower()
    if backend not in SWAP_BACKENDS:
        raise ConfigError(
            "%s.swap_backend %r is not one of: %s"
            % (where, backend_raw, ", ".join(SWAP_BACKENDS))
        )
    gasless = _req_bool(raw, "gasless", False, where)
    if gasless and backend != SWAP_BACKEND_BITGET:
        raise ConfigError(
            "%s.gasless=true requires swap_backend=%r (it is a Bitget SwapX "
            "feature); with the %r backend the wallet pays its own gas and the "
            "flag would be silently ignored"
            % (where, SWAP_BACKEND_BITGET, backend)
        )
    return backend, gasless


def _parse_chain(name: str, raw: Any, armed: bool) -> ChainConfig:
    where = "meme_buy.chains.%s" % name
    if not isinstance(raw, Mapping):
        raise ConfigError("%s must be a mapping" % where)
    _reject_unknown(raw, _CHAIN_KEYS, where)

    # Validated even for a disabled chain: a typo'd backend must surface now,
    # not on the day somebody flips enabled=true.
    backend, gasless = _parse_backend(raw, where)

    enabled = _req_bool(raw, "enabled", False, where)
    if not enabled:
        # A disabled chain is inert; keep whatever is there but never use it.
        return ChainConfig(
            name=name, enabled=False, swap_backend=backend, gasless=gasless
        )

    address = _req_str(raw, "withdraw_address", where)
    if not caps.is_valid_address(name, address):
        raise ConfigError(
            "%s.withdraw_address %r is not a valid %s address" % (where, address, name)
        )

    allow_raw = raw.get("withdraw_allowlist", [address])
    if isinstance(allow_raw, (str, bytes)) or not isinstance(allow_raw, (list, tuple)):
        raise ConfigError("%s.withdraw_allowlist must be a list" % where)
    allowlist = tuple(str(a).strip() for a in allow_raw if str(a).strip())
    for entry in allowlist:
        if not caps.is_valid_address(name, entry):
            raise ConfigError(
                "%s.withdraw_allowlist entry %r is not a valid %s address"
                % (where, entry, name)
            )
    # Defence in depth: the address we would actually use must itself be listed.
    if not caps.check_address_allowlist(address, allowlist):
        raise ConfigError("%s.withdraw_address is not in withdraw_allowlist" % where)

    rpc_url = _req_str(raw, "rpc_url", where)
    if not rpc_url.startswith(("http://", "https://")):
        raise ConfigError("%s.rpc_url must be an http(s) URL" % where)

    min_native = caps._as_amount(raw.get("min_native_balance", 0.0))
    if min_native is None or min_native < 0:
        raise ConfigError("%s.min_native_balance must be a number >= 0" % where)

    slippage = raw.get("slippage_bps")
    if isinstance(slippage, bool) or not isinstance(slippage, int):
        raise ConfigError("%s.slippage_bps must be an integer" % where)
    if slippage <= 0 or slippage > caps.MAX_SLIPPAGE_BPS:
        raise ConfigError(
            "%s.slippage_bps must be in 1..%d, got %d"
            % (where, caps.MAX_SLIPPAGE_BPS, slippage)
        )

    tokens_raw = raw.get("token_allowlist", [])
    if isinstance(tokens_raw, (str, bytes)) or not isinstance(tokens_raw, (list, tuple)):
        raise ConfigError("%s.token_allowlist must be a list" % where)
    tokens = []
    for entry in tokens_raw:
        norm = caps.normalize_address(name, entry)
        if norm is None:
            raise ConfigError(
                "%s.token_allowlist entry %r is not a valid %s address"
                % (where, entry, name)
            )
        if norm not in tokens:
            tokens.append(norm)
    if armed and not tokens:
        # v1 has no discovery: armed with nothing to buy means somebody expects
        # the bot to pick a target on its own.  It never will.
        raise ConfigError(
            "%s.token_allowlist is empty while armed=true (v1 buys only from "
            "the configured allowlist)" % where
        )

    return ChainConfig(
        name=name,
        enabled=True,
        withdraw_address=address,
        withdraw_allowlist=allowlist,
        rpc_url=rpc_url,
        min_native_balance=float(min_native),
        slippage_bps=int(slippage),
        swap_backend=backend,
        gasless=gasless,
        token_allowlist=tuple(tokens),
    )


# ---------- public ----------


def load_meme_buy_config(raw: Any) -> MemeBuyConfig:
    """Validate a config mapping (whole file or just the block) -> MemeBuyConfig."""
    if not isinstance(raw, Mapping):
        raise ConfigError("meme_buy config must be a mapping")
    block = raw.get("meme_buy", raw)
    if not isinstance(block, Mapping):
        raise ConfigError("meme_buy config must be a mapping")
    _reject_unknown(block, _TOP_KEYS, "meme_buy")

    armed = _req_bool(block, "armed", False, "meme_buy")
    dry_run = _req_bool(block, "dry_run", True, "meme_buy")

    per_trade = _req_positive(block, "per_trade_usdt_cap", "meme_buy")
    daily = _req_positive(block, "daily_usdt_cap", "meme_buy")
    if per_trade > daily:
        raise ConfigError(
            "meme_buy.per_trade_usdt_cap (%.8g) exceeds daily_usdt_cap (%.8g)"
            % (per_trade, daily)
        )

    kill_switch = _req_str(block, "kill_switch_file", "meme_buy")
    db_path = block.get("db_path") or ""
    if db_path and not isinstance(db_path, str):
        raise ConfigError("meme_buy.db_path must be a string")

    timeout = caps._as_amount(block.get("arrival_timeout_s", 900.0))
    poll = caps._as_amount(block.get("arrival_poll_interval_s", 15.0))
    if timeout is None or timeout <= 0:
        raise ConfigError("meme_buy.arrival_timeout_s must be a number > 0")
    if poll is None or poll <= 0 or poll > timeout:
        raise ConfigError("meme_buy.arrival_poll_interval_s must be > 0 and <= timeout")

    sub_prefix = _env_prefix(block, "bitget_sub_prefix", DEFAULT_SUB_PREFIX)
    main_prefix = _env_prefix(block, "bitget_main_prefix", DEFAULT_MAIN_PREFIX)
    if sub_prefix == main_prefix:
        # Same prefix == same three files == one key playing both roles, which
        # is exactly the mistake the two-key model exists to prevent.
        raise ConfigError(
            "meme_buy.bitget_sub_prefix and bitget_main_prefix must differ "
            "(they name two DIFFERENT Bitget API keys; a sub key can never "
            "withdraw and a main key must not be the transfer source)"
        )

    chains_raw = block.get("chains", {})
    if not isinstance(chains_raw, Mapping):
        raise ConfigError("meme_buy.chains must be a mapping")
    unknown = sorted(set(chains_raw) - set(caps.SUPPORTED_CHAINS))
    if unknown:
        raise ConfigError("meme_buy.chains: unsupported chain(s): %s" % ", ".join(unknown))

    chains = {name: _parse_chain(name, cfg, armed) for name, cfg in chains_raw.items()}
    if armed and not any(c.enabled for c in chains.values()):
        raise ConfigError("meme_buy: armed=true but no chain is enabled")

    return MemeBuyConfig(
        armed=armed,
        dry_run=dry_run,
        per_trade_usdt_cap=per_trade,
        daily_usdt_cap=daily,
        kill_switch_file=kill_switch,
        db_path=str(db_path),
        arrival_timeout_s=float(timeout),
        arrival_poll_interval_s=float(poll),
        bitget_sub_prefix=sub_prefix,
        bitget_main_prefix=main_prefix,
        chains=chains,
    )


def env_arm_acknowledged(env: Optional[Mapping[str, str]] = None) -> bool:
    """Second, restart-only half of the arming decision.  See module docstring."""
    source = os.environ if env is None else env
    return str(source.get(ARM_ENV_VAR, "")).strip() == "1"
