from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from guardian.crypto import CredentialCipher
from guardian.db import Database
from guardian.events import append_event
from guardian.exchange import SUPPORTED_EXCHANGES, ExchangeClient, TamperSignal, make_client


def key_fingerprint(api_key: str) -> str:
    """sha256 of the plaintext key: lets the platform refuse a purchase key
    from `api_keys` as a monitoring key and the same key being monitored
    twice, without ever storing or comparing the plaintext."""
    return hashlib.sha256((api_key or "").encode()).hexdigest()


# `guardian_accounts.label` is varchar(100) (migration 030). The slug a name
# produces is capped below that so a `-N` uniqueness suffix always fits.
_LABEL_BASE_MAX_LENGTH = 90


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

# Bitget's Get-Account-Info response includes an `authorities` field, confirmed
# (Phase 0 audit, docs/phase0-capability-audit.md) to carry per-module
# read/write permission codes such as 'coor' (read) / 'coow' (write) - an 'r'
# suffix for read, a 'w' suffix for write. The exact full enum of module
# prefixes was NOT verified against a live key. Because of that, this check is
# deliberately conservative and fails CLOSED: any authority token that isn't
# unambiguously read-only, or a response we can't parse at all, is rejected
# rather than assumed safe.
_WRITE_KEYWORDS = ("trade", "withdraw", "transfer", "write")

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


class OnboardingRejected(Exception):
    """Raised when a candidate API key cannot be verified as read-only."""


@dataclass
class OnboardingResult:
    account_id: int
    label: str


def _bitget_read_only(authorities: object) -> tuple[bool, str]:
    if authorities is None:
        return False, "account-info response had no 'authorities' field - cannot verify scope"

    if isinstance(authorities, str):
        tokens = [t.strip() for t in authorities.replace(";", ",").split(",") if t.strip()]
    elif isinstance(authorities, (list, tuple)):
        tokens = [str(t).strip() for t in authorities if str(t).strip()]
    else:
        return False, f"unrecognized 'authorities' field type: {type(authorities).__name__}"

    if not tokens:
        return False, "'authorities' field was empty - cannot verify scope"

    for token in tokens:
        lowered = token.lower()
        if any(keyword in lowered for keyword in _WRITE_KEYWORDS):
            return False, f"authority token '{token}' indicates non-read-only scope"
        if lowered.endswith("r"):
            continue
        if lowered.endswith("w"):
            return False, f"authority token '{token}' has Bitget's documented write-scope suffix"
        return False, f"authority token '{token}' doesn't match a recognized read-only pattern - failing closed"

    return True, "all authority tokens matched a read-only ('...r' suffix) pattern"


# Kept as a backwards-compatible alias (existing tests import this name).
_looks_read_only = _bitget_read_only


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


def verify_read_only(client: ExchangeClient, exchange: str = "bitget") -> tuple[bool, str]:
    """Programmatic, fail-closed check that a candidate key is read-only.
    Two independent checks must both pass:
      1. fetch_balance() succeeds (proves the key has working read access).
      2. the exchange's own key-metadata endpoint reports no write scope.
    Any unexpected error during either check is treated as a rejection, not
    a pass - this is a security gate, not a best-effort probe.
    """
    try:
        client.fetch_balance()
    except TamperSignal as exc:
        return False, f"balance read failed - key may be invalid or lacks account-read scope: {exc}"
    except Exception as exc:  # fail closed: any surprise here is a rejection
        return False, f"balance read raised an unexpected error: {exc}"

    try:
        info = client.fetch_account_info()
    except TamperSignal as exc:
        return False, f"key-metadata call failed: {exc}"
    except Exception as exc:  # fail closed
        return False, f"key-metadata call raised an unexpected error: {exc}"

    payload = info.get("data", info) if isinstance(info, dict) else info
    if exchange == "bybit":
        return _bybit_read_only(payload)
    if exchange == "okx":
        return _okx_read_only(payload)
    if exchange == "binance":
        return _binance_read_only(payload)
    authorities = payload.get("authorities") if isinstance(payload, dict) else None
    return _bitget_read_only(authorities)


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
) -> OnboardingResult:
    """Verify the key is read-only, then store the account under its owner.

    `user_id` is the platform user who owns the account (their `users.email`
    is the primary alert destination); `source` says how it got here (self,
    admin, migrated) and `onboarded_by` who did it when an admin did. The
    key's fingerprint is stored so the same key cannot be monitored twice and
    a purchase key can be refused before it gets here."""
    if topology not in ("independent", "sub_account"):
        raise ValueError(f"unknown topology '{topology}'")
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"unsupported exchange '{exchange}' (supported: {', '.join(SUPPORTED_EXCHANGES)})")
    if user_id is None:
        raise ValueError("a monitored account needs an owner (user_id)")
    if source not in ("self", "admin", "migrated"):
        raise ValueError(f"unknown onboarding source '{source}'")

    client = make_client(exchange, api_key, secret, passphrase)
    ok, reason = verify_read_only(client, exchange)
    if not ok:
        raise OnboardingRejected(
            f"rejected {exchange} key for '{label}': {reason}. Guardian only accepts read-only keys."
        )

    now = datetime.now(timezone.utc).isoformat()
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO guardian_accounts
                (user_id, label, exchange, topology, encrypted_api_key, encrypted_secret, encrypted_passphrase,
                 key_fingerprint, customer_name, alert_phone, alert_email_secondary, alert_phone_secondary,
                 telegram_handle, telegram_handle_secondary, license, sms_alerts_enabled, status,
                 baseline_status, onboarding_source, onboarded_by, onboarded_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active',
                    'seeding', %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                label,
                exchange,
                topology,
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
                source,
                onboarded_by,
                now,
                now,
            ),
        )
        account_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO guardian_poll_state (account_id) VALUES (%s)", (account_id,))

    from guardian.baseline import seed_baseline  # local import avoids a module cycle

    # An account whose history could not be fully seeded is not broken, but it
    # WILL false-positive later on activity the customer has always had - and
    # nothing distinguished it from an account that started complete (#58,
    # group C). Recorded as an event so it is attached to the account forever,
    # not only to whatever log file was open at onboarding.
    gaps = seed_baseline(db, account_id, client)
    with db.cursor() as cur:
        cur.execute("UPDATE guardian_accounts SET baseline_status = %s, updated_at = %s WHERE id = %s",
                    ("incomplete" if gaps else "complete", datetime.now(timezone.utc).isoformat(), account_id))
    for gap in gaps:
        append_event(
            db, account_id, "baseline_seed_incomplete", "HIGH", gap, actor=actor,
        )
    if gaps and operator_alerter is not None:
        operator_alerter.page(
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
