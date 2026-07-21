"""Bitget coin/chain metadata: fetch, cache, and pre-flight a withdrawal amount.

Source of truth is the public, unauthenticated endpoint
    GET /api/v2/spot/public/coins?coin=USDT
documented at https://www.bitget.com/api-doc/spot/market/Get-Coin-List

Per-chain fields used here (all quoted from that page):
    chain             "Chain name"            -- exactly "BEP20" / "SOL"
    needTag           "Need tag"              -- memo/tag chains are REFUSED
    withdrawable      "Withdrawal supported"
    minWithdrawAmount "Minimum withdrawal amount"
    withdrawFee       "Withdrawal transaction fee"
    withdrawMinScale  "Decimal places of withdrawal amount"
    withdrawStep      size must be a whole multiple of this when non-zero
    congestion        "chain network status" (normal / congested)
    contractAddress   the token contract, written in the chain's OWN address
                      encoding -- used to catch a mis-paired (chain, address)

Nothing about the numbers is hardcoded: minimums, fees, scale and step are read
from the exchange and cached. Only the *chain string* is validated against the
exchange's own list, so a typo ("BSC", "SOLANA") can never reach a withdrawal.

FAIL CLOSED: a missing, blank or unparseable field is a REJECT, never a pass.
Absent data is not evidence of safety.
"""

import logging
import time
from decimal import Decimal, InvalidOperation

from bitget_client import BASE_URL, BitgetAPIError, BitgetClient

from .errors import WithdrawalBlockedError

log = logging.getLogger("payout.chains")

COINS_PATH = "/api/v2/spot/public/coins"
CACHE_TTL = 3600  # seconds; fees/minimums change rarely, chain status less rarely
MAX_DECIMALS = 8  # Bitget hard limit -- error 43125

# Every one of these must be present and parseable before we let a size through.
REQUIRED_FIELDS = ("withdrawable", "needTag", "minWithdrawAmount", "withdrawFee",
                   "withdrawMinScale", "congestion")

# Only these spellings count as "no tag needed". Anything else -- "true", None,
# a number, a shape we have never seen -- is treated as "tag required".
NO_TAG = ("false", "0", "no")


def _dec(value):
    """Decimal(value) or None -- None means "unusable", i.e. reject."""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


class CoinChains:
    """Cached view of Bitget's per-coin chain table."""

    def __init__(self, base_url=BASE_URL, timeout=15, ttl=CACHE_TTL, session=None):
        # Unauthenticated client: reuses the repo's request/error plumbing only.
        self._client = BitgetClient(base_url=base_url, timeout=timeout)
        if session is not None:
            self._client.session = session
        self._ttl = ttl
        self._cache = {}  # COIN -> (fetched_at, {CHAIN: info})

    # ---------- fetch / cache ----------

    def chains(self, coin, refresh=False):
        """{CHAIN_UPPER: chain_info} for `coin`, cached for `ttl` seconds."""
        key = str(coin).strip().upper()
        cached = self._cache.get(key)
        if cached and not refresh and time.time() - cached[0] < self._ttl:
            return cached[1]

        try:
            data = self._client._request("GET", COINS_PATH, params={"coin": key}, auth=False)
        except BitgetAPIError as exc:
            # Cannot read the rules -> cannot know the size is legal -> refuse.
            raise WithdrawalBlockedError("METADATA", f"cannot read chain metadata for {key}: {exc}") from exc

        # Shape check first: a dict (or anything else) would otherwise be
        # iterated element by element and die on .get with an AttributeError,
        # which no `except ExchangeAPIError` handler upstream catches.
        if data is not None and not isinstance(data, list):
            raise WithdrawalBlockedError(
                "METADATA", f"{COINS_PATH} returned {type(data).__name__}, expected a list; refusing")
        entry = next((row for row in data or []
                      if isinstance(row, dict) and str(row.get("coin", "")).upper() == key), None)
        if entry is None:
            raise WithdrawalBlockedError("METADATA", f"Bitget does not list coin {key!r} at {COINS_PATH}")
        chains = {str(c.get("chain", "")).upper(): c for c in entry.get("chains") or []
                  if isinstance(c, dict) and c.get("chain")}
        if not chains:
            raise WithdrawalBlockedError("METADATA", f"{key}: {COINS_PATH} returned no chains")

        self._cache[key] = (time.time(), chains)
        log.debug("refreshed %s chain metadata: %s", key, sorted(chains))
        return chains

    def clear(self):
        self._cache.clear()

    # ---------- lookups ----------

    def chain_info(self, coin, chain):
        """Chain metadata dict, or WithdrawalBlockedError if the chain is unknown.

        Matching is case-insensitive but callers should send back
        `info["chain"]` -- Bitget's own spelling ("BEP20", "SOL").
        """
        chains = self.chains(coin)
        info = chains.get(str(chain).strip().upper())
        if info is None:
            raise WithdrawalBlockedError(
                "CHAIN",
                f"{str(coin).upper()}: unknown chain {chain!r}; Bitget spells this coin's chains {sorted(chains)}",
            )
        return info

    @staticmethod
    def address_rejection_reason(info, chain, address):
        """None if `address` can belong to `chain`, else a precise reason.

        The chain's own `contractAddress` is written in that chain's address
        encoding, so it says whether the chain is 0x-hex (EVM) or something else
        (base58, bech32) without this module hardcoding a list of chains. A
        0x... destination on a base58 chain -- or the reverse -- is a mis-paired
        (chain, address) config, and paying it out is unrecoverable. Native
        coins carry no contract; then there is nothing to compare and we say so
        by returning None rather than guessing.
        """
        contract = str(info.get("contractAddress") or "").strip()
        addr = str(address or "").strip()
        if not contract or not addr:
            return None
        if contract.lower().startswith("0x") == addr.lower().startswith("0x"):
            return None
        family = "0x-hex (EVM)" if contract.lower().startswith("0x") else "non-0x (base58/bech32)"
        return (f"{str(chain).upper()}: destination {addr!r} does not look like a {family} address, which is "
                f"the encoding this chain's own token contract {contract!r} uses — this is a mis-paired "
                f"(chain, address) pair and the withdrawal would be IRRECOVERABLE")

    def amount_rejection_reason(self, coin, chain, amount):
        """None if `amount` is withdrawable on `chain`, else a precise reason.

        Checks, in order: chain exists -> metadata complete -> withdrawals
        enabled -> chain needs no memo/tag -> chain not congested -> amount
        positive -> >= minWithdrawAmount -> > withdrawFee -> decimal places
        within withdrawMinScale (and 8) -> multiple of withdrawStep. Each
        failure names the Bitget error code it would otherwise have produced at
        the exchange.
        """
        label = f"{str(coin).upper()}/{str(chain).upper()}"
        try:
            info = self.chain_info(coin, chain)
        except WithdrawalBlockedError as exc:
            return exc.message

        missing = [f for f in REQUIRED_FIELDS if str(info.get(f, "")).strip() == ""]
        if missing:
            return (f"{label}: chain metadata from {COINS_PATH} is missing {', '.join(missing)} — "
                    f"refusing (fail closed: absent data is not proof the withdrawal is safe)")

        if str(info["withdrawable"]).strip().lower() != "true":
            return f"{label}: Bitget reports withdrawable={info['withdrawable']!r}; withdrawals on this chain are disabled right now"

        # Tag/memo chains (EOS, XRP, ATOM, ...) credit the destination's OMNIBUS
        # wallet when the memo is missing, and the payout path sends no `tag`
        # field at all. That loss is unrecoverable, so refuse the chain outright
        # rather than broadcast a tagless withdrawal.
        if str(info["needTag"]).strip().lower() not in NO_TAG:
            return (f"{label}: Bitget reports needTag={info['needTag']!r}; this chain requires an address "
                    f"tag/memo and the withdrawal body carries none — a tagless withdrawal here is "
                    f"IRRECOVERABLE, so it is refused (fail closed)")

        congestion = str(info["congestion"]).strip().lower()
        if congestion != "normal":
            return f"{label}: chain status is congestion={congestion!r}; refusing to broadcast until it returns to 'normal'"

        size = _dec(amount)
        if size is None or size <= 0:
            return f"{label}: amount {amount!r} is not a positive number"

        minimum = _dec(info["minWithdrawAmount"])
        fee = _dec(info["withdrawFee"])
        scale = _dec(info["withdrawMinScale"])
        if minimum is None or fee is None or scale is None:
            return (f"{label}: unparseable metadata (minWithdrawAmount={info['minWithdrawAmount']!r}, "
                    f"withdrawFee={info['withdrawFee']!r}, withdrawMinScale={info['withdrawMinScale']!r})")

        if size < minimum:
            return f"{label}: size {size} is below minWithdrawAmount {minimum} (Bitget would answer 43114)"
        if size <= fee:
            return f"{label}: size {size} does not exceed withdrawFee {fee}; the payout would net nothing (43112)"

        decimals = max(0, -size.as_tuple().exponent)
        allowed = min(int(scale), MAX_DECIMALS)
        if decimals > allowed:
            return f"{label}: size {size} has {decimals} decimal places, chain allows {allowed} (43125) — round down"

        raw_step = info.get("withdrawStep")
        if raw_step is not None and str(raw_step).strip() != "":
            step = _dec(raw_step)
            if step is None:
                # Present but unreadable: we cannot prove the size is a legal
                # multiple, and "cannot prove" is a reject here.
                return f"{label}: unparseable withdrawStep {raw_step!r}; refusing (fail closed)"
            if step > 0 and (size % step) != 0:
                return f"{label}: size {size} is not a whole multiple of withdrawStep {step} (43124)"

        return None


# ---------- module-level convenience (one shared cache) ----------

DEFAULT = CoinChains()


def chain_info(coin, chain):
    """chain_info() against the process-wide cache."""
    return DEFAULT.chain_info(coin, chain)


def amount_rejection_reason(coin, chain, amount):
    """amount_rejection_reason() against the process-wide cache."""
    return DEFAULT.amount_rejection_reason(coin, chain, amount)
