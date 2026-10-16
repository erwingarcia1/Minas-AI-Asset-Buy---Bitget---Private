from __future__ import annotations

import logging
from typing import Any

import ccxt

from guardian import net
from guardian.crypto import redact_secrets

logger = logging.getLogger("guardian.exchange")

# Confirmed against live CCXT source (python/ccxt/bitget.py, master, fetched
# 2026-07-02): 'v2/spot/account/info' exists under private.spot.get (weight 20).
# It returns the CALLING key's own authorities/ips fields - Bitget exposes no
# endpoint to list other keys on the account (see docs/phase0-capability-audit.md
# section 1, "New API key created" row). CCXT Python implicit methods are
# exposed in snake_case per CCXT's documented cross-exchange naming convention.
ACCOUNT_INFO_METHOD = "private_spot_get_v2_spot_account_info"

# Confirmed against live CCXT source (python/ccxt/bybit.py, master, fetched
# 2026-07-05, line 431): 'v5/user/query-api' -> implicit method
# private_get_v5_user_query_api. Returns the CALLING key's own readOnly flag
# (0=read+write, 1=read-only) and permissions object. This is the authoritative
# read-only signal for the onboarding gate (see onboarding.py).
BYBIT_QUERY_API_METHOD = "private_get_v5_user_query_api"

# OKX: GET /api/v5/account/config -> private_get_account_config (okx.py line 393).
# Response data[0].perm is a comma-joined permission string (read_only/trade/
# withdraw) for the CALLING key. Confirmed against CCXT-embedded example
# (okx.py line 1656). Passphrase required.
OKX_ACCOUNT_CONFIG_METHOD = "private_get_account_config"

# Binance: GET /sapi/v1/account/apiRestrictions -> sapi_get_account_apirestrictions
# (binance.py sapi.get map). Returns per-key boolean permission flags for the
# CALLING key. No passphrase. Confirmed against live source + Binance docs.
BINANCE_API_RESTRICTIONS_METHOD = "sapi_get_account_apirestrictions"

SUPPORTED_EXCHANGES = ("bitget", "bybit", "okx", "binance")

# Which field in an account-info payload names the exchange account the calling
# key belongs to (#69). This is what stops a replacement key from pointing the
# Guardian at a *different* account: a key can be genuinely read-only, validate
# perfectly, and still be for somebody else's empty account - at which point we
# poll a quiet account, report "watching", and the real one goes unobserved.
#
# Bitget's `userId` is confirmed: it is in the documented response and in the
# fixtures this suite has always used. The Bybit and OKX candidates are read
# from the same normalized `data` dict their clients already build and are NOT
# verified against a live response; several spellings are listed for that
# reason. Binance's apiRestrictions payload carries no account identifier at
# all, so the tuple is deliberately empty.
#
# Unknown is None, and None is refused by every caller. An exchange we cannot
# identify an account on is one where self-serve key replacement stays with an
# operator - which is the fail-closed half of G-4 applied to a new question.
_ACCOUNT_UID_FIELDS = {
    "bitget": ("userId",),
    "bybit": ("userID", "userId", "uid"),
    "okx": ("uid",),
    "binance": (),
}


def exchange_account_uid(exchange: str, payload: object) -> str | None:
    """The exchange's own id for the account a key belongs to, or None.

    Takes an already-fetched `fetch_account_info()` payload rather than a
    client, because every caller has one in hand: the slow loop reads it every
    ten minutes, onboarding reads it once, and asking the exchange a second
    time would cost a request to learn something already on the table.

    Accepts either the whole `{"data": {...}}` envelope or the inner dict."""
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
    if not isinstance(payload, dict):
        return None
    for field in _ACCOUNT_UID_FIELDS.get(exchange, ()):
        value = payload.get(field)
        # A blank string is not an identity. Neither is 0 - and the falsy-
        # sentinel rule in CONTRIBUTING is what makes that worth writing down:
        # `str(value or "")` would turn a legitimate uid of 0 into "no idea",
        # which fails closed but for a reason that is not true.
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


class TamperSignal(Exception):
    """Raised when the exchange rejects our credentials with an auth/permission
    error - our monitoring key may have been deleted or had its permissions
    changed. This is a first-class detection, not just an ops error."""


class ExchangeClient:
    """Base for the read-only exchange wrappers. Holds credential redaction and
    the auth-error -> TamperSignal mapping shared by every exchange. Subclasses
    only ever expose read methods - Sentinel never calls a trade, withdraw, or
    transfer-out endpoint on a customer key."""

    exchange_id: str = ""

    def __init__(self, exchange, api_key: str, secret: str, password: str = ""):
        self._exchange = exchange
        self._api_key = api_key
        self._secret = secret
        self._password = password

    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._api_key, self._secret, self._password)

    def _call(self, fn, *args, **kwargs) -> Any:
        # Every read this service performs passes through here, which makes it
        # the one place that can promise a test run never reaches a real
        # exchange with a real key (#68). The isinstance check is the whole
        # point: the suite drives these wrappers with fakes, and a fake is not
        # a ccxt client, so only a genuine one is refused.
        if isinstance(self._exchange, ccxt.Exchange):
            net.refuse_if_under_test("an exchange request", self.exchange_id or "an exchange")
        try:
            return fn(*args, **kwargs)
        except (ccxt.AuthenticationError, ccxt.PermissionDenied) as exc:
            raise TamperSignal(self._redact(str(exc))) from exc
        except ccxt.BaseError as exc:
            # Scrub secrets before this propagates anywhere it might be logged.
            raise type(exc)(self._redact(str(exc))) from exc

    # --- read surface every detector consumes; subclasses override as needed --
    def fetch_balance(self) -> dict:
        return self._call(self._exchange.fetch_balance)

    def fetch_withdrawals(self, since: int | None = None) -> list[dict]:
        return self._call(self._exchange.fetch_withdrawals, code=None, since=since)

    def fetch_deposits(self, since: int | None = None) -> list[dict]:
        return self._call(self._exchange.fetch_deposits, code=None, since=since)

    def fetch_closed_orders(
        self,
        since: int | None = None,
        symbol_hints: list[str] | None = None,
        coin_hints: list[str] | None = None,
        acquire=None,
    ) -> list[dict]:
        # symbol_hints/coin_hints matter only on exchanges whose order history
        # is per-symbol (Binance); exchanges with account-wide history ignore
        # them. `acquire(n)` is the poller's rate-limiter hook: clients that
        # fan one logical pull into several HTTP calls charge each extra call,
        # so the egress-IP budget reflects real request volume.
        return self._call(self._exchange.fetch_closed_orders, symbol=None, since=since)

    def fetch_transfers(self, code: str, since: int | None = None, acquire=None) -> list[dict]:
        return self._call(self._exchange.fetch_transfers, code=code, since=since)

    def fetch_tickers(self, symbols: list[str]) -> dict:
        """Public reference prices for off-market fill detection."""
        return self._call(self._exchange.fetch_tickers, symbols=symbols)

    def fetch_ticker(self, symbol: str) -> dict:
        """Single-symbol fallback when a batch fetch_tickers is rejected
        (Bybit refuses mixed spot+derivatives symbol lists)."""
        return self._call(self._exchange.fetch_ticker, symbol)

    def fetch_account_info(self) -> dict:
        raise NotImplementedError


class BitgetClient(ExchangeClient):
    """Thin, read-only wrapper around ccxt.bitget. Never calls a trade,
    withdraw, or transfer endpoint - Sentinel only ever reads."""

    exchange_id = "bitget"

    def __init__(self, api_key: str, secret: str, password: str):
        super().__init__(
            ccxt.bitget(
                {"apiKey": api_key, "secret": secret, "password": password, "enableRateLimit": True}
            ),
            api_key,
            secret,
            password,
        )

    def fetch_my_trades(self, symbol: str, since: int | None = None) -> list[dict]:
        # CCXT's bitget.fetchMyTrades() raises ArgumentsRequired without a
        # symbol for classic (non-UTA) accounts - confirmed against the
        # installed ccxt source. Not usable for an all-symbols reconciliation
        # pull; see fetch_closed_orders() below for that instead.
        return self._call(self._exchange.fetch_my_trades, symbol=symbol, since=since)

    def fetch_account_info(self) -> dict:
        """Native (implicit) call - CCXT has no unified method for this.
        Used for the onboarding read-only permission check and for the
        best-effort 'new API key' proxy detector (see detectors/new_api_key.py)."""
        method = getattr(self._exchange, ACCOUNT_INFO_METHOD, None)
        if method is None:
            raise RuntimeError(
                f"ccxt.bitget has no implicit method '{ACCOUNT_INFO_METHOD}' - "
                "CCXT's Bitget endpoint map may have changed since this was verified "
                "against source on 2026-07-02. Re-check docs/phase0-capability-audit.md."
            )
        return self._call(method)


class BybitClient(ExchangeClient):
    """Thin, read-only wrapper around ccxt.bybit. Bybit v5 has no API
    passphrase. Read-only status is authoritative via the query-api endpoint
    (readOnly flag). See docs/phase0-capability-audit.md (Bybit addendum)."""

    exchange_id = "bybit"
    # Cross-product order coverage: a naive fetch_closed_orders defaults to
    # category=linear (USDT perps) only. Confirmed against CCXT bybit source.
    _ORDER_CATEGORIES = (
        {"type": "spot"},
        {"subType": "linear"},
        {"subType": "inverse"},
        {"type": "option"},
    )
    # Bybit fetch_balance defaults to the CONTRACT wallet; try the widest wallet
    # first. UNIFIED covers a UTA account's whole balance in one call.
    _BALANCE_TYPES = ("unified", "spot", "swap")

    def __init__(self, api_key: str, secret: str, password: str = ""):
        super().__init__(
            ccxt.bybit({"apiKey": api_key, "secret": secret, "enableRateLimit": True}),
            api_key,
            secret,
            "",  # no passphrase on Bybit; nothing to redact
        )

    def fetch_balance(self) -> dict:
        last_exc: Exception | None = None
        for wallet in self._BALANCE_TYPES:
            try:
                return self._call(self._exchange.fetch_balance, params={"type": wallet})
            except TamperSignal:
                raise  # auth failure is a tamper signal, not a wallet-type miss
            except Exception as exc:  # noqa: BLE001 - try the next wallet type
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        return {}

    def fetch_closed_orders(
        self,
        since: int | None = None,
        symbol_hints: list[str] | None = None,
        coin_hints: list[str] | None = None,
        acquire=None,
    ) -> list[dict]:
        orders: list[dict] = []
        for i, params in enumerate(self._ORDER_CATEGORIES):
            if i and acquire is not None:
                acquire(1)  # the poller's block budget covers only the first category
            try:
                orders.extend(
                    self._call(self._exchange.fetch_closed_orders, symbol=None, since=since, params=dict(params))
                )
            except TamperSignal:
                raise
            except Exception as exc:  # noqa: BLE001 - a disabled product category must not kill the pull
                logger.debug("bybit closed-orders %s unavailable: %s", params, self._redact(str(exc)))
        return orders

    def fetch_account_info(self) -> dict:
        """Bybit query-api: the calling key's own readOnly flag, permissions,
        and IP allow-list. Normalized so the shared poller/onboarding code can
        read `data.readOnly`, `data.permissions`, `data.authorities` (aliased to
        permissions for the drift detector), and `data.ips`."""
        method = getattr(self._exchange, BYBIT_QUERY_API_METHOD, None)
        if method is None:
            raise RuntimeError(
                f"ccxt.bybit has no implicit method '{BYBIT_QUERY_API_METHOD}' - "
                "CCXT's Bybit endpoint map may have changed since this was verified "
                "against source on 2026-07-05."
            )
        raw = self._call(method)
        result = raw.get("result", raw) if isinstance(raw, dict) else {}
        data = dict(result) if isinstance(result, dict) else {}
        data.setdefault("authorities", data.get("permissions"))
        return {"data": data}


class OKXClient(ExchangeClient):
    """Read-only wrapper around ccxt.okx. OKX requires an API passphrase (in
    `password`, like Bitget). Read-only status is authoritative via the
    account-config `perm` field. See docs/phase0-capability-audit.md (OKX
    addendum)."""

    exchange_id = "okx"
    # fetch_closed_orders is instType-scoped; a naive call covers only the
    # default (spot). Iterate the product types for account-wide coverage.
    _ORDER_INST_TYPES = ("SPOT", "SWAP", "FUTURES", "OPTION", "MARGIN")

    def __init__(self, api_key: str, secret: str, password: str):
        super().__init__(
            ccxt.okx(
                {"apiKey": api_key, "secret": secret, "password": password, "enableRateLimit": True}
            ),
            api_key,
            secret,
            password,
        )

    def fetch_closed_orders(
        self,
        since: int | None = None,
        symbol_hints: list[str] | None = None,
        coin_hints: list[str] | None = None,
        acquire=None,
    ) -> list[dict]:
        orders: list[dict] = []
        for i, inst_type in enumerate(self._ORDER_INST_TYPES):
            if i and acquire is not None:
                acquire(1)  # the poller's block budget covers only the first instType
            try:
                orders.extend(
                    self._call(self._exchange.fetch_closed_orders, symbol=None, since=since,
                               params={"instType": inst_type})
                )
            except TamperSignal:
                raise
            except Exception as exc:  # noqa: BLE001 - a disabled instType must not kill the pull
                logger.debug("okx closed-orders %s unavailable: %s", inst_type, self._redact(str(exc)))
        return orders

    def fetch_account_info(self) -> dict:
        method = getattr(self._exchange, OKX_ACCOUNT_CONFIG_METHOD, None)
        if method is None:
            raise RuntimeError(
                f"ccxt.okx has no implicit method '{OKX_ACCOUNT_CONFIG_METHOD}' - "
                "CCXT's OKX endpoint map may have changed since this was verified on 2026-07-05."
            )
        raw = self._call(method)
        rows = raw.get("data") if isinstance(raw, dict) else None
        row = rows[0] if isinstance(rows, list) and rows else {}
        data = dict(row) if isinstance(row, dict) else {}
        data.setdefault("authorities", data.get("perm"))
        # OKX reports the key's IP allow-list under 'ip' (singular, comma-
        # joined); normalize to 'ips' so the permission-drift detector sees it.
        data.setdefault("ips", data.get("ip"))
        return {"data": data}


class BinanceClient(ExchangeClient):
    """Read-only wrapper around ccxt.binance (global). No API passphrase.
    Read-only status is authoritative via the apiRestrictions permission
    flags. See docs/phase0-capability-audit.md (Binance addendum)."""

    exchange_id = "binance"
    # Binance order history is per-symbol (fetch_closed_orders requires a
    # symbol), so coverage is only as good as the symbol list. Candidates come
    # from three sources: pairs the account is known to have traded
    # (symbol_hints), coins held on the PREVIOUS poll (coin_hints - so an asset
    # sold to zero still gets its own sell order pulled), and coins held now.
    # Quote coins (incl. BTC) are treated as bases too: a customer selling BTC
    # on BTC/USDT must produce an explaining fill or quantity-drop false-fires.
    _QUOTES = ("USDT", "FDUSD", "BTC")
    _MAX_SYMBOLS = 24
    # Universal-transfer routes to watch. ccxt's fetch_transfers default
    # queries ONLY spot->USD-M-futures, which leaves the route detector blind
    # to funding/margin staging and makes ordinary internal transfers look like
    # unexplained balance drops. One call per route, all assets, cached per
    # client instance (the poller builds a fresh client each loop).
    _TRANSFER_ROUTES = (
        ("spot", "funding"), ("funding", "spot"),
        ("spot", "future"), ("future", "spot"),
        ("spot", "margin"), ("margin", "spot"),
    )

    def __init__(self, api_key: str, secret: str, password: str = ""):
        super().__init__(
            ccxt.binance({"apiKey": api_key, "secret": secret, "enableRateLimit": True}),
            api_key,
            secret,
            "",  # no passphrase on Binance
        )
        self._transfers_cache: list[dict] | None = None

    def fetch_closed_orders(
        self,
        since: int | None = None,
        symbol_hints: list[str] | None = None,
        coin_hints: list[str] | None = None,
        acquire=None,
    ) -> list[dict]:
        if acquire is not None:
            acquire(1)  # the internal fetch_balance below
        try:
            totals = self.fetch_balance().get("total", {})
        except TamperSignal:
            raise
        except Exception:  # noqa: BLE001
            totals = {}

        candidates: list[str] = []
        seen: set[str] = set()

        def add(symbol: str | None) -> None:
            if symbol and symbol not in seen:
                seen.add(symbol)
                candidates.append(symbol)

        # Held (and previously-held) coins FIRST: their fills are what
        # quantity-drop reconciles, so if the budget truncates anything it must
        # be stale history hints, never current coverage.
        coins = [c for c, v in totals.items() if v] + [c for c in (coin_hints or ()) if c]
        for base in coins:
            for quote in self._QUOTES:
                if base != quote:
                    add(f"{base}/{quote}")
        held_syms = set(candidates)
        for symbol in symbol_hints or ():
            add(symbol)

        # Drop nonexistent markets before spending private-call weight on them.
        # load_markets is cached on the instance and ccxt would run it inside
        # the first fetch_closed_orders call anyway.
        try:
            if acquire is not None:
                acquire(1)
            markets = self._call(self._exchange.load_markets)
            candidates = [s for s in candidates if s in markets]
        except TamperSignal:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to trial-and-error per symbol
            logger.debug("binance load_markets failed: %s", self._redact(str(exc)))

        if len(candidates) > self._MAX_SYMBOLS:
            held_dropped = sum(1 for s in candidates[self._MAX_SYMBOLS:] if s in held_syms)
            if held_dropped:
                logger.warning(
                    "binance closed-orders: %d held-coin symbols beyond the %d-symbol budget - "
                    "quantity-drop coverage gap this cycle",
                    held_dropped, self._MAX_SYMBOLS,
                )
            else:
                logger.debug(
                    "binance closed-orders: dropping %d stale hint symbols beyond the %d-symbol budget",
                    len(candidates) - self._MAX_SYMBOLS, self._MAX_SYMBOLS,
                )
            candidates = candidates[: self._MAX_SYMBOLS]

        orders: list[dict] = []
        for i, symbol in enumerate(candidates):
            if i and acquire is not None:
                acquire(1)  # first pull rides the poller's block budget
            try:
                orders.extend(self._call(self._exchange.fetch_closed_orders, symbol=symbol, since=since))
            except TamperSignal:
                raise
            except Exception as exc:  # noqa: BLE001 - a non-existent market must not kill the pull
                logger.debug("binance closed-orders %s unavailable: %s", symbol, self._redact(str(exc)))
        return orders

    def fetch_transfers(self, code: str, since: int | None = None, acquire=None) -> list[dict]:
        if self._transfers_cache is None:
            rows: list[dict] = []
            for i, (from_acct, to_acct) in enumerate(self._TRANSFER_ROUTES):
                if i and acquire is not None:
                    acquire(1)  # the poller charges 1 per coin; extra routes charge here
                try:
                    rows.extend(
                        self._call(
                            self._exchange.fetch_transfers, code=None, since=since,
                            params={"fromAccount": from_acct, "toAccount": to_acct},
                        )
                    )
                except TamperSignal:
                    raise
                except Exception as exc:  # noqa: BLE001 - a disabled wallet must not kill the pull
                    logger.debug(
                        "binance transfers %s->%s unavailable: %s", from_acct, to_acct, self._redact(str(exc))
                    )
            self._transfers_cache = rows
        if code is None:
            return list(self._transfers_cache)
        return [t for t in self._transfers_cache if t.get("currency") == code]

    def fetch_account_info(self) -> dict:
        method = getattr(self._exchange, BINANCE_API_RESTRICTIONS_METHOD, None)
        if method is None:
            raise RuntimeError(
                f"ccxt.binance has no implicit method '{BINANCE_API_RESTRICTIONS_METHOD}' - "
                "CCXT's Binance endpoint map may have changed since this was verified on 2026-07-05."
            )
        raw = self._call(method)
        data = dict(raw) if isinstance(raw, dict) else {}
        # authorities = the set of enabled flags, for the permission-drift detector.
        enabled = sorted(k for k, v in data.items() if isinstance(v, bool) and v)
        data.setdefault("authorities", enabled)
        return {"data": data}


def make_client(exchange: str, api_key: str, secret: str, passphrase: str = "") -> ExchangeClient:
    """Factory: builds the read-only client for a given exchange id."""
    if exchange == "bitget":
        return BitgetClient(api_key, secret, passphrase)
    if exchange == "bybit":
        return BybitClient(api_key, secret)
    if exchange == "okx":
        return OKXClient(api_key, secret, passphrase)
    if exchange == "binance":
        return BinanceClient(api_key, secret)
    raise ValueError(f"unsupported exchange '{exchange}' (supported: {', '.join(SUPPORTED_EXCHANGES)})")
