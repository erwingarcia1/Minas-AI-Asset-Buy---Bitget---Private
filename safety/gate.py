"""TOKEN SAFETY GATE -- the fail-closed pre-trade screen that runs before any spend.

``screen_token()`` runs 17 numbered checks in cheap-first order and returns a
GateVerdict.  It performs only read-only GETs; it never moves money and cannot
raise -- every exception, timeout, HTTP 429, non-200 and MISSING FIELD becomes a
FAILED check.  The orchestrator must treat ``verdict.passed is not True`` as a
hard stop, including when the verdict came back with an internal error.

Order (see the numbered check names in the verdict):
  1  address in the local config allowlist
  2  address format (EIP-55 checksummed EVM / base58 32-byte Solana mint)
  BSC
  3  is_open_source == "1"
  4  is_honeypot == "0" AND cannot_sell_all == "0" AND cannot_buy == "0"
  5  buy_tax <= 10% and sell_tax <= 10%   (GoPlus taxes are FRACTIONS: "0.05" == 5%)
  6  none of the mutable/owner flags set
  7  >= 95% of LP locked or burned
  8  honeypot.is simulation succeeded, not a honeypot, riskLevel < 12
  SOLANA
  9  mintAuthority null AND freezeAuthority null
  10 no dangerous Token-2022 extension (unknown extension == dangerous)
  11 no RugCheck risk at level "danger" and score_normalised <= 20
  12 lpLockedPct >= 95% and every locker's unlock date is far enough out
  BOTH
  13 top-10 holders < 30% excluding LP vaults / lockers / burn
  14 liquidity >= $50,000
  15 pair age >= 24h
  16 24h volume >= $25k, volume/liquidity in [0.5, 10], txns.m5.sells > 0
  17 quoted price impact <= 5% (passed in by the caller from the DEX quote)

Every field a check reads must be PRESENT.  GoPlus omits fields it cannot
determine -- verified live, PancakeSwap's own CAKE returned no cannot_sell_all,
no buy_tax, no sell_tax and no lp_holders.  A gate written
``if data.get("is_honeypot") == "1": reject`` FAILS OPEN on exactly the thin,
brand-new tokens this bot looks at.  Absent => REJECT, reason "field X missing".

LIMITATIONS -- read this before trusting a green verdict
--------------------------------------------------------
* It cannot catch a SLOW RUG.  Nothing here models a team quietly selling into
  the book over days, or LP that is locked today and unlocks next month.
  Unlock timers are parsed, but a lock that merely outlasts the threshold is
  still a countdown, not safety.
* It cannot see INSIDER OR BUNDLED WALLETS.  Supply split across 200 fresh
  addresses funded from one source passes the top-10 concentration check.
* A clean scan is valid for roughly ONE BLOCK.  Everything is read before the
  buy and can change in the block the buy lands in, including by a sandwich or
  by the owner front-running the bot's own transaction.
* If ANY mutable flag is set, the owner can raise the sell tax to 100%, pause
  transfers, or blacklist a specific address AFTER the buy settles.  Checks 5
  and 6 describe the token's state now, not its permissions later.
* On Solana a live freezeAuthority can freeze YOUR token account after the
  purchase; the position becomes unsellable while still showing a price.
* SIMULATORS CANNOT SEE TRAPS THAT HAVE NOT FIRED.  honeypot.is buys and sells
  against current state; a trap gated on block height, holder count, or a
  whitelist simulates clean.  This has a structural consequence: the early
  buyer is the one whose loss creates the signal every other tool reads.
  Being first through this gate is not an edge, it is the sample.
* This gate is a PUBLIC ADVERSARIAL TARGET.  GoPlus, honeypot.is, RugCheck and
  DexScreener are free and scriptable, so scammers pre-test contracts against
  them until they come back clean.  A passing score partly selects for scams
  competent enough to pass.
* The DOMINANT LOSS MODE is not scored here at all: a fully sellable, fully
  legitimate token whose price simply goes to zero. Every check above is about
  whether you CAN sell, none is about whether it will be WORTH anything.

Configuration
-------------
The allowlist is loaded from the JSON file named by ``SAFETY_ALLOWLIST_FILE``
(the env var holds a PATH):  ``{"bsc": ["0x.."], "solana": ["Mint.."]}``.
No allowlist configured => every token is rejected.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import dexscreener, goplus, honeypot_is, rugcheck, solana_mint
from ._address import is_checksum_address, is_valid_solana_mint
from .errors import (AddressFormatError, AllowlistError, MissingFieldError,
                     SafetyProviderError)

ALLOWLIST_ENV = "SAFETY_ALLOWLIST_FILE"
SUPPORTED_CHAINS = ("bsc", "solana")

# Sanity window for DexScreener's ``pairCreatedAt`` (milliseconds since epoch).
EARLIEST_PAIR_MS = 1_420_070_400_000   # 2015-01-01, older than any AMM pair
CLOCK_SKEW_SECONDS = 300               # tolerance for a pair "created" just ahead of us

# GoPlus BSC flags that must ALL be present and "0".
MUTABLE_FLAGS: Tuple[str, ...] = (
    "is_proxy", "slippage_modifiable", "personal_slippage_modifiable",
    "transfer_pausable", "is_blacklisted", "hidden_owner",
    "can_take_back_ownership", "selfdestruct", "is_mintable",
    "anti_whale_modifiable",
)
BURN_ADDRESSES = frozenset({
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
    "11111111111111111111111111111111",
})
# Tag hints are matched as WHOLE WORDS, never as substrings.  Substring matching
# reads "Unlocked team wallet" as locked (it contains "lock") and drops a whale
# tagged "Alpaca Finance" from the concentration sum (it contains "lp") -- both
# flip a check from REJECT to PASS, which is the one direction that costs money.
_LOCK_TAG_HINTS = ("lock", "locks", "locked", "locker", "lockers", "burn", "burns",
                   "burned", "burnt", "black hole", "blackhole", "null", "dead")
_POOL_TAG_HINTS = _LOCK_TAG_HINTS + ("lp", "pair", "pairs", "pool", "pools",
                                     "vault", "vaults", "amm")
_TAG_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------- verdict ----------

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class GateVerdict:
    passed: bool
    checks: List[CheckResult] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        head = "PASS" if self.passed else "REJECT"
        return f"{head} ({len(self.checks)} checks)" + (
            ": " + "; ".join(self.reasons) if self.reasons else "")


@dataclass(frozen=True)
class Thresholds:
    """Every gate constant, all overridable by the caller."""
    max_buy_tax: float = 0.10            # fraction, matching GoPlus units
    max_sell_tax: float = 0.10
    min_lp_locked_fraction: float = 0.95  # BSC, GoPlus lp_holders percents
    max_honeypot_is_risk_level: int = 12  # strict <
    min_rugcheck_lp_locked_pct: float = 95.0
    min_lp_unlock_seconds: int = 30 * 86400
    max_rugcheck_score_normalised: float = 20.0  # HIGHER = WORSE
    max_top10_holder_fraction: float = 0.30
    min_liquidity_usd: float = 50_000.0
    min_pair_age_seconds: int = 24 * 3600
    min_volume_h24_usd: float = 25_000.0
    min_volume_liquidity_ratio: float = 0.5
    max_volume_liquidity_ratio: float = 10.0
    min_txns_m5_sells: int = 1
    max_price_impact_bps: int = 500      # 5%


@dataclass
class Providers:
    """Injectable data sources -- swapped wholesale in tests, never networked there."""
    goplus_evm: Callable[[str], Dict[str, Any]]
    goplus_solana: Callable[[str], Dict[str, Any]]
    honeypot_is: Callable[[str], Any]
    rugcheck: Callable[[str], Any]
    dexscreener: Callable[[str, str], Any]
    solana_mint: Callable[[str], Any]


def default_providers() -> Providers:
    return Providers(
        goplus_evm=goplus.fetch_evm,
        goplus_solana=goplus.fetch_solana,
        honeypot_is=honeypot_is.fetch,
        rugcheck=rugcheck.fetch,
        dexscreener=dexscreener.fetch,
        solana_mint=solana_mint.fetch,
    )


# ---------- allowlist ----------

def load_allowlist(source: Optional[Any] = None) -> Dict[str, set]:
    """Load {chain: {addresses}} from a mapping, a path, or SAFETY_ALLOWLIST_FILE."""
    raw: Any = source
    if raw is None or isinstance(raw, str):
        path = raw or os.environ.get(ALLOWLIST_ENV)
        if not path:
            raise AllowlistError("not_configured",
                                 f"no token allowlist configured ({ALLOWLIST_ENV} unset)")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            raise AllowlistError("unreadable",
                                 f"cannot load allowlist: {type(exc).__name__}") from None
    if not isinstance(raw, Mapping):
        raise AllowlistError("bad_format", "allowlist must be {chain: [addresses]}")

    out: Dict[str, set] = {}
    for chain, addresses in raw.items():
        if str(chain).startswith("_"):
            continue  # "_comment" style keys -- JSON has no comments
        if isinstance(addresses, (str, bytes)) or not isinstance(addresses, Iterable):
            raise AllowlistError("bad_format", f"allowlist[{chain}] is not a list")
        out[str(chain).lower()] = {str(a).strip() for a in addresses if str(a).strip()}
    return out


# ---------- ledger ----------

class _Ledger:
    def __init__(self) -> None:
        self.checks: List[CheckResult] = []

    def add(self, name: str, passed: bool, detail: Any) -> bool:
        self.checks.append(CheckResult(name, bool(passed), str(detail)))
        return bool(passed)

    def run(self, name: str, fn: Callable[[], Tuple[bool, str]]) -> bool:
        """Run a check; ANY exception is a failed check, never a raise."""
        try:
            passed, detail = fn()
        except Exception as exc:  # noqa: BLE001 - fail closed on everything
            return self.add(name, False, f"{type(exc).__name__}: {exc}")
        return self.add(name, passed, detail)

    def skip_all(self, names: Sequence[str], detail: str) -> None:
        for name in names:
            self.add(name, False, detail)


# ---------- field helpers (absence is never "safe") ----------

def _flag(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name)
    if value is None or value == "":
        raise MissingFieldError(name, "goplus")
    return str(value)


def _fraction(data: Mapping[str, Any], name: str) -> float:
    raw = _flag(data, name)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise MissingFieldError(name, "goplus") from None


def _entry_address(entry: Mapping[str, Any]) -> str:
    return str(entry.get("address") or entry.get("account") or "").lower()


def _entry_percent(entry: Mapping[str, Any], where: str) -> float:
    value = entry.get("percent")
    if value is None or value == "":
        raise MissingFieldError(f"{where}[].percent", "goplus")
    try:
        percent = float(value)
    except (TypeError, ValueError):
        raise MissingFieldError(f"{where}[].percent", "goplus") from None
    # GoPlus percents are FRACTIONS ("0.97" == 97%), the same units as the
    # thresholds.  If that ever changes under us, "5" would read as 5% instead
    # of 500% and a single LP holder would satisfy the 95%-locked rule alone.
    # Out of range means the units moved: reject rather than guess.
    if not 0.0 <= percent <= 1.0 + 1e-9:
        raise SafetyProviderError(
            "bad_units",
            f"{where}[].percent={value!r} is not a 0..1 fraction (GoPlus units changed?)")
    return percent


def _tag_hits(tag: Any, hints: Sequence[str]) -> bool:
    """Whole-word tag match -- see the note on _LOCK_TAG_HINTS."""
    words = _TAG_WORD_RE.findall(str(tag).lower())
    joined = " ".join(words)
    for hint in hints:
        if " " in hint:
            if hint in joined:
                return True
        elif hint in words:
            return True
    return False


def _is_locked_entry(entry: Mapping[str, Any]) -> bool:
    locked = str(entry.get("is_locked", "0")).lower() in ("1", "true")
    return locked or _entry_address(entry) in BURN_ADDRESSES or \
        _tag_hits(entry.get("tag", ""), _LOCK_TAG_HINTS)


def _require_list(data: Mapping[str, Any], name: str) -> List[Mapping[str, Any]]:
    value = data.get(name)
    if not isinstance(value, list) or not value:
        raise MissingFieldError(name, "goplus")
    return value


def _lp_locked_fraction(lp_holders: Sequence[Mapping[str, Any]]) -> float:
    return sum(_entry_percent(h, "lp_holders") for h in lp_holders if _is_locked_entry(h))


def _top10_holder_fraction(holders: Sequence[Mapping[str, Any]],
                           lp_addresses: Iterable[str]) -> float:
    """Top-10 circulating concentration, excluding pools, lockers and burns."""
    pools = {a.lower() for a in lp_addresses}
    shares: List[float] = []
    for holder in holders:
        pct = _entry_percent(holder, "holders")  # assert presence even if skipped
        addr = _entry_address(holder)
        if addr in pools or addr in BURN_ADDRESSES or \
                _tag_hits(holder.get("tag", ""), _POOL_TAG_HINTS):
            continue
        shares.append(pct)
    shares.sort(reverse=True)
    return sum(shares[:10])


# ---------- chain branches ----------

_BSC_CHECKS = ("03_goplus_open_source", "04_goplus_honeypot_flags", "05_goplus_taxes",
               "06_goplus_mutable_flags", "07_lp_locked_or_burned", "13_holder_concentration")
_SOL_GOPLUS_CHECKS = ("13_holder_concentration",)
_SOL_RPC_CHECKS = ("09_solana_authorities", "10_token2022_extensions")
_SOL_RUGCHECK_CHECKS = ("11_rugcheck_risk", "12_rugcheck_lp_lock")
_MARKET_CHECKS = ("14_liquidity", "15_pair_age", "16_volume_and_flow")

# A verdict may only pass if every rule for the chain actually RAN.  Without
# this, a code path that quietly skips a check would read as a clean scan.
_REQUIRED = {
    "bsc": ("01_allowlist", "02_address_format") + _BSC_CHECKS +
           ("08_honeypot_is_simulation",) + _MARKET_CHECKS + ("17_price_impact",),
    "solana": ("01_allowlist", "02_address_format") + _SOL_RPC_CHECKS +
              _SOL_RUGCHECK_CHECKS + _SOL_GOPLUS_CHECKS + _MARKET_CHECKS +
              ("17_price_impact",),
}


def _screen_bsc(ledger: _Ledger, address: str, th: Thresholds, pr: Providers) -> None:
    try:
        data = pr.goplus_evm(address)
        if not isinstance(data, Mapping) or not data:
            raise TypeError("GoPlus returned a non-mapping payload")
        ledger.add("goplus_fetch", True, f"{len(data)} fields")
    except Exception as exc:  # noqa: BLE001
        ledger.add("goplus_fetch", False, f"{type(exc).__name__}: {exc}")
        ledger.skip_all(_BSC_CHECKS, "no GoPlus data")
        data = None

    if data is not None:
        ledger.run("03_goplus_open_source", lambda: (
            _flag(data, "is_open_source") == "1",
            f"is_open_source={_flag(data, 'is_open_source')}"))

        def _honeypot_flags():
            values = {n: _flag(data, n) for n in ("is_honeypot", "cannot_sell_all", "cannot_buy")}
            bad = [f"{n}={v}" for n, v in values.items() if v != "0"]
            return (not bad, ", ".join(bad) if bad else "all three flags 0")
        ledger.run("04_goplus_honeypot_flags", _honeypot_flags)

        def _taxes():
            buy, sell = _fraction(data, "buy_tax"), _fraction(data, "sell_tax")
            ok = buy <= th.max_buy_tax and sell <= th.max_sell_tax
            return (ok, f"buy_tax={buy:.4f} sell_tax={sell:.4f} (fractions; "
                        f"max {th.max_buy_tax:.4f}/{th.max_sell_tax:.4f})")
        ledger.run("05_goplus_taxes", _taxes)

        def _mutable():
            # ``!= "0"`` and not ``== "1"``: GoPlus documents 0/1, but an
            # unexpected value ("true", "2", a future enum) must not read as
            # "flag clear".  Only a literal "0" is permission-free.
            set_flags = [f"{n}={_flag(data, n)}" for n in MUTABLE_FLAGS
                         if _flag(data, n) != "0"]
            return (not set_flags, ", ".join(set_flags) if set_flags else "no mutable flags set")
        ledger.run("06_goplus_mutable_flags", _mutable)

        def _lp():
            locked = _lp_locked_fraction(_require_list(data, "lp_holders"))
            return (locked >= th.min_lp_locked_fraction,
                    f"lp locked/burned {locked * 100:.2f}% "
                    f"(min {th.min_lp_locked_fraction * 100:.2f}%)")
        ledger.run("07_lp_locked_or_burned", _lp)

        def _holders():
            lp_addrs = [_entry_address(h) for h in data.get("lp_holders") or []]
            top10 = _top10_holder_fraction(_require_list(data, "holders"), lp_addrs)
            return (top10 < th.max_top10_holder_fraction,
                    f"top-10 non-pool holders {top10 * 100:.2f}% "
                    f"(max {th.max_top10_holder_fraction * 100:.2f}%)")
        ledger.run("13_holder_concentration", _holders)

    def _simulate():
        report = pr.honeypot_is(address)
        problems = []
        if not report.simulation_success:
            problems.append("simulation failed")
        if report.is_honeypot:
            problems.append("flagged honeypot")
        if report.risk_level >= th.max_honeypot_is_risk_level:
            problems.append(f"riskLevel {report.risk_level} >= {th.max_honeypot_is_risk_level}")
        return (not problems,
                ", ".join(problems) if problems else
                f"simulated sell ok, riskLevel {report.risk_level}")
    ledger.run("08_honeypot_is_simulation", _simulate)


def _screen_solana(ledger: _Ledger, mint: str, th: Thresholds, pr: Providers) -> None:
    try:
        report = pr.solana_mint(mint)
        ledger.add("solana_mint_fetch", True, f"program {report.program_id}")
    except Exception as exc:  # noqa: BLE001
        ledger.add("solana_mint_fetch", False, f"{type(exc).__name__}: {exc}")
        ledger.skip_all(_SOL_RPC_CHECKS, "no mint account data")
        report = None

    if report is not None:
        def _authorities():
            live = []
            if report.mint_authority:
                live.append("mintAuthority live (supply can be inflated)")
            if report.freeze_authority:
                live.append("freezeAuthority live (your account can be frozen after the buy)")
            return (not live, ", ".join(live) if live else "mint+freeze authority both null")
        ledger.run("09_solana_authorities", _authorities)

        def _extensions():
            if report.program_id not in solana_mint.KNOWN_TOKEN_PROGRAMS:
                return (False, f"unknown token program {report.program_id}")
            findings = solana_mint.dangerous_extensions(report)
            return (not findings, "; ".join(findings) if findings else
                    f"{len(report.extensions)} extension(s), none dangerous")
        ledger.run("10_token2022_extensions", _extensions)

    try:
        rc = pr.rugcheck(mint)
        ledger.add("rugcheck_fetch", True, f"score_normalised {rc.score_normalised}")
    except Exception as exc:  # noqa: BLE001
        ledger.add("rugcheck_fetch", False, f"{type(exc).__name__}: {exc}")
        ledger.skip_all(_SOL_RUGCHECK_CHECKS, "no RugCheck data")
        rc = None

    if rc is not None:
        def _risk():
            # score_normalised is HIGHER = WORSE (USDC 1, BONK 7, live-authority 76).
            problems = []
            if rc.danger_risks:
                problems.append("danger risks: " + ", ".join(rc.danger_risks))
            if rc.score_normalised > th.max_rugcheck_score_normalised:
                problems.append(f"score_normalised {rc.score_normalised} > "
                                f"{th.max_rugcheck_score_normalised} (higher is worse)")
            return (not problems, ", ".join(problems) if problems else
                    f"score_normalised {rc.score_normalised} (higher is worse), no danger risks")
        ledger.run("11_rugcheck_risk", _risk)

        def _lp_lock():
            if rc.lp_locked_pct is None:
                raise MissingFieldError("lpLockedPct", "rugcheck")
            problems = []
            if rc.lp_locked_pct < th.min_rugcheck_lp_locked_pct:
                problems.append(f"lpLockedPct {rc.lp_locked_pct:.2f} < "
                                f"{th.min_rugcheck_lp_locked_pct:.2f}")
            floor = time.time() + th.min_lp_unlock_seconds
            for locker in rc.lockers:
                if not locker.unlock_date:
                    # 0 / absent == unverifiable. Conservative: reject.
                    problems.append(f"locker {locker.owner[:8]} has no unlock date")
                elif locker.unlock_date < floor:
                    problems.append(f"locker {locker.owner[:8]} unlocks in "
                                    f"{int((locker.unlock_date - time.time()) // 3600)}h")
            return (not problems, ", ".join(problems) if problems else
                    f"lpLockedPct {rc.lp_locked_pct:.2f}, {len(rc.lockers)} locker(s) beyond floor")
        ledger.run("12_rugcheck_lp_lock", _lp_lock)

    try:
        data = pr.goplus_solana(mint)
        if not isinstance(data, Mapping) or not data:
            raise TypeError("GoPlus returned a non-mapping payload")
        ledger.add("goplus_fetch", True, f"{len(data)} fields")
    except Exception as exc:  # noqa: BLE001
        ledger.add("goplus_fetch", False, f"{type(exc).__name__}: {exc}")
        ledger.skip_all(_SOL_GOPLUS_CHECKS, "no GoPlus data")
        data = None

    if data is not None:
        def _holders():
            lp_addrs = [_entry_address(h) for h in data.get("lp_holders") or []]
            top10 = _top10_holder_fraction(_require_list(data, "holders"), lp_addrs)
            return (top10 < th.max_top10_holder_fraction,
                    f"top-10 non-pool holders {top10 * 100:.2f}% "
                    f"(max {th.max_top10_holder_fraction * 100:.2f}%)")
        ledger.run("13_holder_concentration", _holders)


def _screen_market(ledger: _Ledger, chain: str, address: str,
                   th: Thresholds, pr: Providers) -> None:
    try:
        pair = pr.dexscreener(address, chain)
        ledger.add("dexscreener_fetch", True, f"pair {pair.pair_address} on {pair.dex_id}")
    except Exception as exc:  # noqa: BLE001
        ledger.add("dexscreener_fetch", False, f"{type(exc).__name__}: {exc}")
        ledger.skip_all(_MARKET_CHECKS, "no DexScreener data")
        return

    def _liquidity():
        if pair.liquidity_usd is None:
            raise MissingFieldError("liquidity.usd", "dexscreener")
        return (pair.liquidity_usd >= th.min_liquidity_usd,
                f"liquidity ${pair.liquidity_usd:,.0f} (min ${th.min_liquidity_usd:,.0f})")
    ledger.run("14_liquidity", _liquidity)

    def _age():
        if not pair.pair_created_at_ms:
            raise MissingFieldError("pairCreatedAt", "dexscreener")
        created_ms = float(pair.pair_created_at_ms)
        now = time.time()
        # pairCreatedAt is MILLISECONDS.  A seconds-epoch value (or any other
        # garbage) would date every brand-new pair to the 1970s and turn the
        # single most useful anti-rug filter into a rubber stamp, so the
        # timestamp has to land in a window a real DEX pair could occupy.
        if not math.isfinite(created_ms) or not (
                EARLIEST_PAIR_MS <= created_ms <= (now + CLOCK_SKEW_SECONDS) * 1000.0):
            return (False, f"pairCreatedAt {pair.pair_created_at_ms!r} is not a plausible "
                           f"millisecond timestamp")
        age = now - created_ms / 1000.0
        return (age >= th.min_pair_age_seconds,
                f"pair age {age / 3600:.1f}h (min {th.min_pair_age_seconds / 3600:.1f}h)")
    ledger.run("15_pair_age", _age)

    def _flow():
        if pair.volume_h24_usd is None:
            raise MissingFieldError("volume.h24", "dexscreener")
        if pair.liquidity_usd is None:
            raise MissingFieldError("liquidity.usd", "dexscreener")
        if pair.txns_m5_sells is None or pair.txns_m5_buys is None:
            raise MissingFieldError("txns.m5.buys/sells", "dexscreener")
        # NaN compares False against everything, so an unusable count would
        # slip past "sells < 1" as if there had been sells.
        for name, value in (("buys", pair.txns_m5_buys), ("sells", pair.txns_m5_sells)):
            if not math.isfinite(float(value)):
                raise MissingFieldError(f"txns.m5.{name}", "dexscreener")
        problems = []
        if pair.volume_h24_usd < th.min_volume_h24_usd:
            problems.append(f"24h volume ${pair.volume_h24_usd:,.0f} < "
                            f"${th.min_volume_h24_usd:,.0f}")
        if pair.liquidity_usd <= 0:
            problems.append("liquidity is zero")
        else:
            ratio = pair.volume_h24_usd / pair.liquidity_usd
            if not (th.min_volume_liquidity_ratio <= ratio <= th.max_volume_liquidity_ratio):
                problems.append(f"volume/liquidity {ratio:.2f} outside "
                                f"[{th.min_volume_liquidity_ratio}, "
                                f"{th.max_volume_liquidity_ratio}]")
        # Many buys and zero sells is a honeypot tell that needs no security API.
        if pair.txns_m5_sells < th.min_txns_m5_sells:
            problems.append(f"m5 sells {pair.txns_m5_sells} with {pair.txns_m5_buys} buys")
        return (not problems, ", ".join(problems) if problems else
                f"24h volume ${pair.volume_h24_usd:,.0f}, m5 {pair.txns_m5_buys}b/"
                f"{pair.txns_m5_sells}s")
    ledger.run("16_volume_and_flow", _flow)


# ---------- public ----------

def screen_token(chain: str,
                 address: str,
                 price_impact_bps: Optional[int] = None,
                 thresholds: Optional[Thresholds] = None,
                 providers: Optional[Providers] = None,
                 allowlist: Optional[Any] = None) -> GateVerdict:
    """Run the full pre-trade screen.  Read-only, fail-closed, never raises."""
    ledger = _Ledger()
    try:
        th = thresholds or Thresholds()
        pr = providers or default_providers()
        chain_key = (chain or "").strip().lower()
        addr = (address or "").strip()

        if chain_key not in SUPPORTED_CHAINS:
            ledger.add("00_chain_supported", False,
                       f"unsupported chain {chain!r} (want one of {SUPPORTED_CHAINS})")
            return _finalise(ledger)

        # 1 -- allowlist. Cheapest check and the only one that is not advisory:
        # a token that is not pre-approved is never bought, however clean it looks.
        def _allowlist():
            table = load_allowlist(allowlist)
            entries = table.get(chain_key, set())
            hit = addr in entries or (chain_key == "bsc" and
                                      addr.lower() in {e.lower() for e in entries})
            return (hit, f"{addr} {'in' if hit else 'NOT in'} {chain_key} allowlist "
                         f"({len(entries)} entries)")
        if not ledger.run("01_allowlist", _allowlist):
            return _finalise(ledger)

        # 2 -- format. EVM must be EIP-55 checksummed; Solana must decode to 32 bytes.
        def _format():
            if chain_key == "bsc":
                if not is_checksum_address(addr):
                    raise AddressFormatError(
                        "bad_address", f"{addr} is not an EIP-55 checksummed EVM address")
                return (True, "EIP-55 checksum valid")
            if not is_valid_solana_mint(addr):
                raise AddressFormatError(
                    "bad_address", f"{addr} is not a base58 32-byte Solana mint")
            return (True, "base58 32-byte mint")
        if not ledger.run("02_address_format", _format):
            return _finalise(ledger)

        if chain_key == "bsc":
            _screen_bsc(ledger, addr, th, pr)
        else:
            _screen_solana(ledger, addr, th, pr)

        _screen_market(ledger, chain_key, addr, th, pr)

        def _impact():
            if price_impact_bps is None or isinstance(price_impact_bps, bool):
                return (False, "price impact not supplied by the caller (DEX quote required)")
            # float, not int: int() truncates TOWARDS the pass, so 500.99 bps
            # used to clear a 500 bps cap.
            bps = float(price_impact_bps)
            if not math.isfinite(bps):
                return (False, f"price impact {price_impact_bps!r} is not a finite number")
            return (0.0 <= bps <= th.max_price_impact_bps,
                    f"price impact {bps:g} bps (max {th.max_price_impact_bps} bps)")
        ledger.run("17_price_impact", _impact)
        return _finalise(ledger, _REQUIRED[chain_key])

    except Exception as exc:  # noqa: BLE001 - the gate must be incapable of raising
        ledger.add("gate_internal_error", False, f"{type(exc).__name__}: {exc}")
    return _finalise(ledger)


def _finalise(ledger: _Ledger, required: Sequence[str] = ()) -> GateVerdict:
    checks = ledger.checks
    ran = {c.name for c in checks}
    for name in required:
        if name not in ran:
            ledger.add("gate_incomplete", False, f"check {name} never ran")
    failed = [c for c in checks if not c.passed]
    return GateVerdict(
        passed=bool(checks) and not failed,
        checks=checks,
        reasons=[f"{c.name}: {c.detail}" for c in failed],
    )


__all__ = ["screen_token", "GateVerdict", "CheckResult", "Thresholds", "Providers",
           "default_providers", "load_allowlist", "MUTABLE_FLAGS", "SUPPORTED_CHAINS",
           "ALLOWLIST_ENV"]
