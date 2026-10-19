from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2.errors

from api.services.bitget_permissions import guardian_bitget_read_only
from guardian.crypto import CredentialCipher, redact_secrets
from guardian.db import Database
from guardian.events import append_event
from guardian.exchange import SUPPORTED_EXCHANGES, ExchangeClient, TamperSignal, exchange_account_uid, make_client

logger = logging.getLogger("guardian.onboarding")


def key_fingerprint(api_key: str) -> str:
    """sha256 of the plaintext key: lets the platform refuse a purchase key
    from `api_keys` as a monitoring key and the same key being monitored
    twice, without ever storing or comparing the plaintext."""
    return hashlib.sha256((api_key or "").encode()).hexdigest()


# `guardian_accounts.label` is varchar(100) (migration 030). The slug a name
# produces is capped below that so a `-N` uniqueness suffix always fits.
_LABEL_BASE_MAX_LENGTH = 90

# `guardian_accounts.customer_name` is varchar(255).
_CUSTOMER_NAME_MAX_LENGTH = 255


def generate_unique_label(db: Database, name: str) -> str:
    """Turn a customer name into a unique account label (slug). The label is
    the stable identifier used across the system; the full name is stored
    separately as customer_name."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "customer"
    # Capped before the lookup, so earlier labels made from the same long name
    # are found. Unbounded, a long name failed the INSERT with a truncation
    # error after the exchange key had already been verified.
    base = base[:_LABEL_BASE_MAX_LENGTH].rstrip("-")
    with db.cursor() as cur:
        cur.execute("SELECT label FROM guardian_accounts WHERE label = %s OR label LIKE %s", (base, base + "-%"))
        taken = {r["label"] for r in cur.fetchall()}
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


# Bitget's read-only verdict comes from the platform's confirmed permission
# table (api/services/bitget_permissions.py, shared with key verification on
# the trading side) since v0.9.2. Guardian's own gate accepted only codes
# ending in 'r' and refused everything ending in 'w', which turned away the
# read scopes `taxw` (Taxation -> Search) and `p2pw` (P2P -> Search) that an
# honest read-only key carries, and it had no notion of a Unified Trading
# Account key (permType / permissions from /api/v3/account/info). The table
# fixes both and still fails CLOSED on an empty list and on codes nobody has
# confirmed. See `_bitget_payload_read_only` for how the payload's shape says
# which kind of key answered.

# Bybit v5: the `permissions` object on query-api lists SCOPES (which endpoint
# groups the key can access), NOT write grants - Bybit's own apikey-info docs
# (bybit-exchange.github.io/docs/v5/user/apikey-info, verified 2026-07-06) show
# a readOnly:1 key listing ContractTrade:[Order,Position], Spot:[SpotTrade],
# Wallet:[AccountTransfer], and permissions are mandatory at key creation
# regardless of readOnly. Reading wallet balance / order history REQUIRES those
# scopes, so treating them as write grants would reject every usable read-only
# key. readOnly==1 is the platform-enforced write bar; the only scope a
# monitoring key must never carry is withdrawal - that one still fails CLOSED.
_BYBIT_WRITE_TOKENS = ("withdraw",)


# What a customer reads when a key is refused: what happened and what to do.
# The technical reason (which code, which field) goes to the log and to
# `OnboardingRejected.reason`, never to the screen - the person reading the
# screen may be doing so over an attacker's shoulder, and "authority token
# 'stow'" helps nobody but us.
NOT_READ_ONLY_MESSAGE = (
    "That key was refused because it is not read-only, or because we could not read the account "
    "with it. Create a new key on the exchange with read permission only - no trading, no "
    "withdrawals, no transfers - and try again."
)
# Bitget answers 40085 ("You are in Unified Account mode, and the Classic Account API is
# not supported") to a Classic (v2) call made with a Unified Trading account's key. Guardian's
# Bitget client reads the v2 endpoints, so such a key cannot be watched yet; the customer is
# told that plainly instead of the generic "not read-only" refusal.
BITGET_UNIFIED_REFUSAL_CODE = "40085"
# The code as a number standing on its own. ccxt's error text carries the whole response,
# requestTime included, and a plain substring match read the 40085 inside
# `"requestTime":1740085123456` as the refusal.
_UNIFIED_REFUSAL_RE = re.compile(rf"(?<!\d){BITGET_UNIFIED_REFUSAL_CODE}(?!\d)")
UNIFIED_REASON_PREFIX = "unified_account:"
UNIFIED_NOT_SUPPORTED_MESSAGE = (
    "This Bitget key belongs to a Unified Trading account, which Guardian cannot watch yet: "
    "it reads the Classic account endpoints. Use a key from a Classic account for now; "
    "Unified support is coming."
)

ALREADY_MONITORED_MESSAGE = (
    "This exchange account is already being watched as {label}. Add a different account, or "
    "replace the key on the existing one."
)


class OnboardingRejected(Exception):
    """Raised when a candidate API key cannot be verified as read-only, or
    the account may not be added for another reason the customer can act on.

    `str(exc)` is the plain-language message for the customer; `reason` is
    the technical detail for the log."""

    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class AlreadyMonitored(OnboardingRejected):
    """The same exchange account (same owner, same exchange, same exchange
    uid) is already watched by a row that is not disabled. `label` names
    that row, so the customer can find it."""

    def __init__(self, message: str, label: str):
        super().__init__(message, reason=f"exchange account already monitored as '{label}'")
        self.label = label


@dataclass
class OnboardingResult:
    account_id: int
    label: str


@dataclass
class _Verdict:
    """What the two-step read-only check found. `payload` is the inner
    account-info dict (the exchange uid and the account mode are read from
    it) and `account_mode` is 'classic' / 'unified' for a Bitget key when the
    payload's shape says so, else None."""

    ok: bool
    reason: str
    payload: object = None
    account_mode: str | None = None


def _bitget_read_only(authorities: object) -> tuple[bool, str]:
    """Classic Bitget key: the verdict comes from the shared permission table."""
    return guardian_bitget_read_only(authorities)


# Kept as a backwards-compatible alias (existing tests import this name).
_looks_read_only = _bitget_read_only


def _bitget_payload_read_only(payload: object) -> tuple[bool, str, str | None]:
    """Bitget account-info payload -> (ok, reason, account_mode).

    A Unified Trading Account answers /api/v3/account/info with `permType`
    and `permissions`; a Classic account answers /api/v2/spot/account/info
    with `authorities`. The shape tells the mode and the table decides the
    verdict. A payload that is neither fails closed and has no mode."""
    if not isinstance(payload, dict):
        return False, f"account-info response had unexpected type: {type(payload).__name__}", None
    perm_type = payload.get("permType", payload.get("perm_type"))
    if perm_type is not None:
        permissions = payload.get("permissions", payload.get("authorities"))
        ok, reason = guardian_bitget_read_only(permissions, perm_type=perm_type)
        return ok, reason, "unified"
    ok, reason = _bitget_read_only(payload.get("authorities"))
    return ok, reason, ("classic" if "authorities" in payload else None)


def _bybit_read_only(data: object) -> tuple[bool, str]:
    """Bybit v5 query-api gate. readOnly==1 is the authoritative, documented
    read-only signal; the permissions scan is defense-in-depth. Any ambiguity
    fails CLOSED."""
    if not isinstance(data, dict):
        return False, f"query-api response had unexpected type: {type(data).__name__}"

    read_only = data.get("readOnly")
    try:
        if int(read_only) != 1:
            return False, f"Bybit readOnly flag is {read_only!r} (expected 1 = read-only) - key has write access"
    except (TypeError, ValueError):
        return False, f"Bybit readOnly flag missing or unparseable ({read_only!r}) - failing closed"

    # Defense-in-depth: contradict the flag if any write action is granted.
    permissions = data.get("permissions")
    if isinstance(permissions, dict):
        for category, actions in permissions.items():
            if not isinstance(actions, (list, tuple)):
                continue
            for action in actions:
                lowered = str(action).lower()
                if any(tok in lowered for tok in _BYBIT_WRITE_TOKENS):
                    return False, (
                        f"Bybit key grants write action '{action}' under '{category}' "
                        f"despite readOnly flag - failing closed"
                    )

    return True, "Bybit readOnly flag is 1 and no write permissions are granted"


# OKX perm tokens (comma-joined string on account-config). Only 'read_only'
# is acceptable; 'trade'/'withdraw' are write. Confirmed against CCXT-embedded
# example (docs/phase0-capability-audit.md, OKX addendum).
_OKX_WRITE_TOKENS = ("trade", "withdraw")

# Binance apiRestrictions: enableReading must be true; every one of these
# write-implying flags must be present AND false. A missing flag fails CLOSED.
# Confirmed against Binance apikey-permission docs (Binance addendum).
_BINANCE_WRITE_FLAGS = (
    "enableWithdrawals", "enableInternalTransfer", "enableMargin", "enableFutures",
    "permitsUniversalTransfer", "enableVanillaOptions", "enableFixApiTrade",
    "enableSpotAndMarginTrading", "enablePortfolioMarginTrading",
)

# The only boolean flags that may be true on a read-only Binance key. Binance
# has repeatedly ADDED trade-capable flags (portfolio margin, FIX trade), so an
# unrecognized enabled flag is treated as a possible new write grant and fails
# CLOSED rather than assumed benign.
_BINANCE_BENIGN_TRUE_FLAGS = ("enableReading", "ipRestrict", "enableFixReadOnly")


def _okx_read_only(data: object) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, f"account-config response had unexpected type: {type(data).__name__}"
    perm = data.get("perm")
    if not isinstance(perm, str) or not perm.strip():
        return False, f"OKX perm field missing or empty ({perm!r}) - failing closed"
    tokens = [t.strip().lower() for t in perm.split(",") if t.strip()]
    for token in tokens:
        if token in _OKX_WRITE_TOKENS:
            return False, f"OKX key has '{token}' permission - not read-only"
        if token != "read_only":
            return False, f"OKX key has unrecognized permission token '{token}' - failing closed"
    if "read_only" not in tokens:
        return False, "OKX perm did not include 'read_only' - failing closed"
    return True, "OKX perm is read_only with no trade/withdraw tokens"


def _binance_read_only(data: object) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, f"apiRestrictions response had unexpected type: {type(data).__name__}"
    if data.get("enableReading") is not True:
        return False, f"Binance enableReading is {data.get('enableReading')!r} (expected true) - not a read key"
    for flag in _BINANCE_WRITE_FLAGS:
        value = data.get(flag)
        if value is None:
            return False, f"Binance flag '{flag}' missing from response - failing closed"
        if value is not False:
            return False, f"Binance key grants '{flag}' ({value!r}) - not read-only"
    for flag, value in data.items():
        if value is True and flag not in _BINANCE_BENIGN_TRUE_FLAGS and flag not in _BINANCE_WRITE_FLAGS:
            return False, f"Binance key has unrecognized enabled flag '{flag}' - failing closed"
    return True, "Binance enableReading=true and all write flags are false"


def _is_unified_refusal(exc: BaseException) -> bool:
    """Bitget's 40085: a Classic (v2) endpoint called with a Unified Trading
    account's key. The code must stand alone as a number in the text
    (`"code":"40085"`, `40085`), never as the digits of a longer one."""
    return _UNIFIED_REFUSAL_RE.search(str(exc)) is not None


def _verify(client: ExchangeClient, exchange: str) -> _Verdict:
    """The check behind `verify_read_only`, keeping the account-info payload
    so onboarding can read the exchange uid and the account mode from the
    same response instead of asking the exchange twice."""
    try:
        client.fetch_balance()
    except TamperSignal as exc:
        if _is_unified_refusal(exc):
            return _Verdict(False, f"{UNIFIED_REASON_PREFIX} balance read refused with {BITGET_UNIFIED_REFUSAL_CODE}")
        return _Verdict(False, f"balance read failed - key may be invalid or lacks account-read scope: {exc}")
    except Exception as exc:  # fail closed: any surprise here is a rejection
        if _is_unified_refusal(exc):
            return _Verdict(False, f"{UNIFIED_REASON_PREFIX} balance read refused with {BITGET_UNIFIED_REFUSAL_CODE}")
        return _Verdict(False, f"balance read raised an unexpected error: {exc}")

    try:
        info = client.fetch_account_info()
    except TamperSignal as exc:
        if _is_unified_refusal(exc):
            return _Verdict(False, f"{UNIFIED_REASON_PREFIX} key-metadata call refused with {BITGET_UNIFIED_REFUSAL_CODE}")
        return _Verdict(False, f"key-metadata call failed: {exc}")
    except Exception as exc:  # fail closed
        if _is_unified_refusal(exc):
            return _Verdict(False, f"{UNIFIED_REASON_PREFIX} key-metadata call refused with {BITGET_UNIFIED_REFUSAL_CODE}")
        return _Verdict(False, f"key-metadata call raised an unexpected error: {exc}")

    payload = info.get("data", info) if isinstance(info, dict) else info
    if exchange == "bybit":
        ok, reason = _bybit_read_only(payload)
        return _Verdict(ok, reason, payload)
    if exchange == "okx":
        ok, reason = _okx_read_only(payload)
        return _Verdict(ok, reason, payload)
    if exchange == "binance":
        ok, reason = _binance_read_only(payload)
        return _Verdict(ok, reason, payload)
    ok, reason, account_mode = _bitget_payload_read_only(payload)
    return _Verdict(ok, reason, payload, account_mode)


def verify_read_only(client: ExchangeClient, exchange: str = "bitget") -> tuple[bool, str]:
    """Programmatic, fail-closed check that a candidate key is read-only.
    Two independent checks must both pass:
      1. fetch_balance() succeeds (proves the key has working read access).
      2. the exchange's own key-metadata endpoint reports no write scope.
    Any unexpected error during either check is treated as a rejection, not
    a pass - this is a security gate, not a best-effort probe.
    """
    verdict = _verify(client, exchange)
    return verdict.ok, verdict.reason


def _set_baseline_status(db: Database, account_id: int, status: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET baseline_status = %s, updated_at = %s WHERE id = %s",
                    (status, datetime.now(timezone.utc).isoformat(), account_id))


def _page(alerter, **kwargs) -> None:
    """Page an operator, and never let a paging failure change the outcome:
    the row's status has been written by the time this runs, and a dead mail
    server must not turn a seeded account into an exception."""
    if alerter is None:
        return
    try:
        alerter.page(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not page an operator about %s: %s", kwargs.get("rule"), exc)


def _record_baseline_outcome(db: Database, account_id: int, label: str, gaps: list[dict], *,
                             actor: str | None, operator_alerter) -> str:
    """Write what seeding found: the row's baseline_status, one event per
    gap, and a page when the history could only be read in part. Returns the
    status written ('complete' or 'incomplete')."""
    status = "incomplete" if gaps else "complete"
    _set_baseline_status(db, account_id, status)
    # An account whose history could not be fully seeded is not broken, but it
    # WILL false-positive later on activity the customer has always had - and
    # nothing distinguished it from an account that started complete (#58,
    # group C). Recorded as an event so it is attached to the account forever,
    # not only to whatever log file was open at onboarding.
    for gap in gaps:
        append_event(
            db, account_id, "baseline_seed_incomplete", "HIGH", gap, actor=actor,
        )
    if gaps:
        _page(
            operator_alerter,
            rule="account_onboarded_with_partial_baseline",
            severity="HIGH",
            message=(
                f"{label} was onboarded but {len(gaps)} part(s) of its history could not be "
                f"read from the exchange. The account is monitored, but detectors that compare "
                f"against history will raise false positives on activity this customer has "
                f"always had. Re-run the seed once the exchange endpoint is working."
            ),
            evidence={"account_label": label, "gaps": gaps},
            account_id=account_id,
            dedup_scope=f"onboard:{label}",
        )
    return status


def _record_seed_failure(db: Database, account_id: int, label: str, exc: BaseException, *,
                         secrets: tuple[str, ...], actor: str | None, operator_alerter) -> str:
    """A seed that raised. The row must not stay 'seeding', whatever happened:
    it is marked 'incomplete' and a 'baseline_seed_failed' event says why,
    with every secret redacted, and an operator is paged to re-seed it. The
    account is then polled like any other incomplete one. Returns the status
    written, 'incomplete'."""
    reason = redact_secrets(f"{type(exc).__name__}: {exc}", *secrets)[:300]
    logger.warning("account %s: baseline seeding failed: %s", label, reason)
    try:
        _set_baseline_status(db, account_id, "incomplete")
        append_event(
            db, account_id, "baseline_seed_failed", "HIGH",
            {"error": reason,
             "consequence": "the account is polled without a seeded history; detectors that "
                            "compare against history may false-positive until it is re-seeded"},
            actor=actor,
        )
    except Exception as inner:  # noqa: BLE001
        logger.error("account %s: could not record the failed seed: %s", label, inner)
    _page(
        operator_alerter,
        rule="account_baseline_seed_failed",
        severity="HIGH",
        message=(
            f"{label}: the baseline could not be seeded ({reason}). The account is marked "
            f"incomplete and is being polled, but detectors that compare against history may "
            f"false-positive on activity this customer has always had. Re-run the seed."
        ),
        evidence={"account_label": label, "error": reason},
        account_id=account_id,
        dedup_scope=f"seed:{label}",
    )
    return "incomplete"


def _seed_or_mark_incomplete(db: Database, account_id: int, label: str, client: ExchangeClient, *,
                             secrets: tuple[str, ...], actor: str | None, operator_alerter) -> str:
    """Seed the baseline with `client` and record what it found; on ANY
    exception the row is marked 'incomplete' rather than left 'seeding'
    (`_record_seed_failure`). Both seed paths - `onboard_account(seed=True)`
    and `seed_account_baseline` - end here, so neither can strand a row.
    Returns the status written: 'complete' or 'incomplete'."""
    try:
        from guardian.baseline import seed_baseline  # local import avoids a module cycle

        gaps = seed_baseline(db, account_id, client)
        return _record_baseline_outcome(db, account_id, label, gaps, actor=actor, operator_alerter=operator_alerter)
    except Exception as exc:  # noqa: BLE001 - the row must not stay 'seeding', whatever happened
        return _record_seed_failure(db, account_id, label, exc, secrets=secrets, actor=actor,
                                    operator_alerter=operator_alerter)


def seed_account_baseline(db: Database, cipher: CredentialCipher, account_id: int, *,
                          actor: str | None = None, operator_alerter=None) -> str:
    """Seed an account's baseline from its stored credentials and return the
    baseline_status written: 'complete' or 'incomplete'.

    This is the second half of `onboard_account(seed=False)`: the API inserts
    the row in the request and runs this in the background, because seeding
    makes several exchange calls and a customer should not wait on them. The
    engine does not poll a row while it says 'seeding', so a row must never
    be left there: on ANY exception - the exchange refusing, the database
    going away, a credential that no longer decrypts - the row is marked
    'incomplete' and a 'baseline_seed_failed' event says why, with every
    secret redacted. The account is then polled like any other incomplete
    one, and an operator is paged to re-seed it. The engine's scheduler calls
    this too, for a row the API left 'seeding' when it died between the 201
    and its background task (`guardian.engine`)."""
    label = f"account {account_id}"
    secrets: tuple[str, ...] = ()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT label, exchange, encrypted_api_key, encrypted_secret, encrypted_passphrase "
                "FROM guardian_accounts WHERE id = %s",
                (account_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError("no such account")
        label = row["label"]
        if not row["encrypted_api_key"]:
            raise RuntimeError("the account has no monitoring key on record")
        api_key = cipher.decrypt(row["encrypted_api_key"])
        secret = cipher.decrypt(row["encrypted_secret"])
        passphrase = cipher.decrypt(row["encrypted_passphrase"]) if row["encrypted_passphrase"] else ""
        secrets = tuple(s for s in (api_key, secret, passphrase) if s)
        client = make_client(row["exchange"], api_key, secret, passphrase)
    except Exception as exc:  # noqa: BLE001 - the row must not stay 'seeding', whatever happened
        return _record_seed_failure(db, account_id, label, exc, secrets=secrets, actor=actor,
                                    operator_alerter=operator_alerter)
    return _seed_or_mark_incomplete(db, account_id, label, client, secrets=secrets, actor=actor,
                                    operator_alerter=operator_alerter)


# The label is chosen before the exchange checks (`generate_unique_label` in
# the API route), and those checks take seconds; two customers adding the same
# name at once can both reach the INSERT holding one label. The constraint
# catches it, and the INSERT is retried once with a label chosen after it.
_LABEL_CONSTRAINT = "uq_guardian_accounts_label"


def _is_label_collision(exc: BaseException) -> bool:
    return getattr(getattr(exc, "diag", None), "constraint_name", None) == _LABEL_CONSTRAINT


def onboard_account(
    db: Database,
    cipher: CredentialCipher,
    *,
    label: str,
    topology: str,
    api_key: str,
    secret: str,
    passphrase: str,
    exchange: str = "bitget",
    user_id=None,
    source: str = "self",
    onboarded_by=None,
    actor: str | None = None,
    operator_alerter=None,
    alert_phone: str | None = None,
    customer_name: str | None = None,
    alert_email_secondary: str | None = None,
    alert_phone_secondary: str | None = None,
    telegram_handle: str | None = None,
    telegram_handle_secondary: str | None = None,
    license: str | None = None,
    sms_alerts_enabled: bool = True,
    seed: bool = True,
) -> OnboardingResult:
    """Verify the key is read-only, then store the account under its owner.

    `user_id` is the platform user who owns the account (their `users.email`
    is the primary alert destination); `source` says how it got here (self,
    admin, migrated) and `onboarded_by` who did it when an admin did. The
    key's fingerprint is stored so the same key cannot be monitored twice and
    a purchase key can be refused before it gets here.

    With `seed=True` (the default, and what the CLI and the engine tests do)
    the baseline is seeded here and the row leaves as 'complete' or
    'incomplete' - never 'seeding': a seed that raises marks the row
    'incomplete' and records why, exactly as `seed_account_baseline` does.
    With `seed=False` the row is inserted as 'seeding', which the engine does
    not poll, and the caller runs `seed_account_baseline` afterwards - the
    API does so in a background task so the customer is not kept waiting on
    the exchange.

    Raises `OnboardingRejected` (plain-language `str`, detail in `.reason`)
    when the key is not read-only, and its subclass `AlreadyMonitored` when
    this owner already watches the same exchange account with a key that is
    not disabled."""
    if topology not in ("independent", "sub_account"):
        raise ValueError(f"unknown topology '{topology}'")
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"unsupported exchange '{exchange}' (supported: {', '.join(SUPPORTED_EXCHANGES)})")
    if user_id is None:
        raise ValueError("a monitored account needs an owner (user_id)")
    if source not in ("self", "admin", "migrated"):
        raise ValueError(f"unknown onboarding source '{source}'")

    client = make_client(exchange, api_key, secret, passphrase)
    verdict = _verify(client, exchange)
    if not verdict.ok:
        logger.warning("refused %s key for '%s': %s", exchange, label, verdict.reason)
        message = UNIFIED_NOT_SUPPORTED_MESSAGE if verdict.reason.startswith(UNIFIED_REASON_PREFIX) else NOT_READ_ONLY_MESSAGE
        raise OnboardingRejected(message, reason=verdict.reason)

    # One exchange account, one monitoring key per owner. The uid comes from
    # the payload the gate just verified, so no second request is made; an
    # exchange whose payload carries no account id (Binance) cannot be
    # checked and is not. A disabled row does not count: a customer who
    # switched monitoring off may add the account again with a fresh key.
    exchange_uid = exchange_account_uid(exchange, verdict.payload)
    if exchange_uid is not None:
        with db.cursor() as cur:
            cur.execute(
                "SELECT label FROM guardian_accounts WHERE user_id = %s AND exchange = %s AND exchange_uid = %s "
                "AND status <> 'disabled' ORDER BY id LIMIT 1",
                (user_id, exchange, exchange_uid),
            )
            existing = cur.fetchone()
        if existing is not None:
            raise AlreadyMonitored(ALREADY_MONITORED_MESSAGE.format(label=existing["label"]), label=existing["label"])

    # Bitget Classic vs Unified, when the permission payload said which. The
    # poller needs no mode today (the client is the same), but a Unified
    # account answers the Classic endpoints with 40085 once migrated, and the
    # column is where that difference will be read from.
    account_mode = verdict.account_mode if exchange == "bitget" else None
    if customer_name is not None:
        customer_name = customer_name.strip()[:_CUSTOMER_NAME_MAX_LENGTH] or None

    now = datetime.now(timezone.utc).isoformat()

    def _insert(label_value: str) -> int:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO guardian_accounts
                    (user_id, label, exchange, topology, account_mode, encrypted_api_key, encrypted_secret,
                     encrypted_passphrase, key_fingerprint, customer_name, alert_phone, alert_email_secondary,
                     alert_phone_secondary, telegram_handle, telegram_handle_secondary, license, sms_alerts_enabled,
                     status, exchange_uid, baseline_status, onboarding_source, onboarded_by, onboarded_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s,
                        'seeding', %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    label_value,
                    exchange,
                    topology,
                    account_mode,
                    cipher.encrypt(api_key),
                    cipher.encrypt(secret),
                    cipher.encrypt(passphrase or ""),
                    key_fingerprint(api_key),
                    customer_name,
                    alert_phone,
                    alert_email_secondary,
                    alert_phone_secondary,
                    telegram_handle,
                    telegram_handle_secondary,
                    license,
                    bool(sms_alerts_enabled),
                    exchange_uid,
                    source,
                    onboarded_by,
                    now,
                    now,
                ),
            )
            new_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (new_id,))
            return new_id

    try:
        account_id = _insert(label)
    except psycopg2.errors.UniqueViolation as exc:
        # Only the label is retried. The key fingerprint's constraint is the
        # route's to explain (the same key on a row the duplicate check skips).
        if not _is_label_collision(exc):
            raise
        fresh = generate_unique_label(db, label)
        logger.info("label '%s' was taken while the %s key was being verified - using '%s'", label, exchange, fresh)
        label = fresh
        account_id = _insert(label)

    if seed:
        _seed_or_mark_incomplete(
            db, account_id, label, client,
            secrets=tuple(s for s in (api_key, secret, passphrase) if s),
            actor=actor, operator_alerter=operator_alerter,
        )

    # The act of onboarding is itself something a person did, and until G-50
    # it left no trace at all - no event row, so no "who added this customer".
    # `actor` is None when it came from the CLI on a system with no operator
    # accounts, which is honest: there was nobody to name.
    append_event(
        db, account_id, "customer_added", "LOW",
        {"label": label, "exchange": exchange, "topology": topology},
        actor=actor,
    )

    return OnboardingResult(account_id=account_id, label=label)
