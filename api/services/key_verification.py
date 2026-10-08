"""Exchange API key verification: one harmless authenticated call, then the
outcome is ALWAYS written to the row and logged.

Outcome vocabulary (``result``):

* ``ok``                  – authenticated; spot trading allowed (or not reported)
* ``invalid_key``         – the venue rejected the key/secret/passphrase/signature
* ``ip_not_allowed``      – the venue rejected this server's IP (allowlist)
* ``no_trade_permission`` – authenticated, but the key cannot trade spot
* ``unreachable``         – transport failure / non-JSON answer from the venue
* ``error``               – any other venue error (code + message recorded)

Every outcome updates ``verification_status`` (verified | failed),
``verification_error`` (readable, never credentials), ``last_checked_at``,
``verified_at`` on success, ``withdrawal_enabled`` when the venue reports
permissions, and the ``verified`` boolean the executor gates on.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from api.models import APIKey
from api.services.exchange_client import ExchangeClient, ExchangeClientError

logger = logging.getLogger("autobuy.exchange")

# Venue error codes that mean "these credentials are wrong" / "your IP is not allowed".
INVALID_CODES = {
    "binance": {"-2014", "-2015", "-1022", "-1002"},
    # 40006 invalid ACCESS_KEY · 40009 signature error · 40011/40012 wrong passphrase
    # · 40037 apikey does not exist · 40014 incorrect permissions (treated below)
    "bitget": {"40006", "40009", "40011", "40012", "40037", "40013"},
}
IP_CODES = {
    "bitget": {"40018"},          # Invalid IP
    "binance": set(),             # Binance folds it into -2015 ("Invalid API-key, IP, or permissions")
}
TRANSPORT_CODES = {"NETWORK", "HTTP"}

VENUE_NAMES = {"binance": "Binance", "bitget": "Bitget"}
UNIFIED_CODES = {"bitget": {"40085"}}   # "You are in Unified Account mode, and the Classic Account API is not supported"


def unified_message() -> str:
    """Customer-facing text for a migrated account; the support address comes from settings (SUPPORT_EMAIL)."""
    from api.config import settings
    return ("This Bitget account uses Unified Trading mode, which AutoBuy doesn't support yet. We're adding it now. "
            f"Please contact {settings.support_email or 'support'} and we'll let you know as soon as it's ready.")
READ_ONLY_MESSAGE = ("This key is Read-only, so AutoBuy can't place buys. In Bitget, edit the key to Read-write "
                     "with only Spot → Trade ticked, then verify again.")


@dataclass
class Verification:
    verified: bool
    result: str
    detail: str
    withdrawal_enabled: Optional[bool] = None
    error_code: Optional[str] = None
    permissions: list = field(default_factory=list)   # codes the venue reported (never credentials)
    extra_permissions: list = field(default_factory=list)   # recognised codes AutoBuy does not need


def classify(exchange: str, exc: ExchangeClientError) -> tuple[str, str]:
    """(result, readable detail) for a venue error. Never includes credentials:
    the venue's own message is about codes, IPs and signatures."""
    code = str(exc.code)
    msg = str(exc).strip()
    lowered = msg.lower()
    venue = VENUE_NAMES.get(exchange, exchange)
    if code in TRANSPORT_CODES:
        return "unreachable", f"{venue} unreachable ({code}): {msg[:200]}"
    if code in IP_CODES.get(exchange, set()) or ("ip" in lowered and ("whitelist" in lowered or "allow" in lowered or "invalid ip" in lowered)):
        return "ip_not_allowed", (f"{venue} rejected this server's IP address (error {code}): {msg[:160]}. "
                                  "Add the server's IP to the key's allowlist, or create the key without an IP restriction.")
    if code in INVALID_CODES.get(exchange, set()) or exchange == "binance" and code == "-2015":
        return "invalid_key", (f"{venue} rejected the credentials (error {code}): {msg[:160]}. "
                               "Check the key, secret" + (", passphrase" if exchange == "bitget" else "")
                               + " and, on Binance, the IP allowlist.")
    return "error", f"{venue} error {code}: {msg[:200]}"


def _client_for(key: APIKey, client_factory=None) -> ExchangeClient:
    """Venue client for a stored key; credentials are decrypted here and never logged."""
    from api.routes.exchange import decrypt_value
    factory = client_factory or ExchangeClient
    return factory(decrypt_value(key.encrypted_api_key), decrypt_value(key.encrypted_api_secret),
                   passphrase=decrypt_value(key.encrypted_passphrase) if key.encrypted_passphrase else "",
                   exchange=key.exchange)


def _check(client: ExchangeClient) -> dict:
    return client.get_account_info()


async def verify_api_key(db, key: APIKey, *, client_factory=None) -> Verification:
    """Run the check and persist the outcome on ``key``. Never raises for venue
    problems; only unexpected programming errors propagate."""
    client = _client_for(key, client_factory)
    now = datetime.now(timezone.utc)
    venue = VENUE_NAMES.get(key.exchange, key.exchange)
    log_ctx = {"user_id": str(key.user_id), "key_id": str(key.id), "exchange": key.exchange}

    try:
        info = await asyncio.to_thread(_check, client)
    except ExchangeClientError as exc:
        if str(exc.code) in UNIFIED_CODES.get(key.exchange, set()):
            result, detail = "unified_account", unified_message()   # v0.7.7: the account was migrated to Unified Trading mode
        else:
            result, detail = classify(key.exchange, exc)
        outcome = Verification(verified=False, result=result, detail=detail, error_code=str(exc.code))
        _record(key, outcome, now)
        await db.commit()
        logger.warning("api_key_verification_failed",
                       extra={**log_ctx, "result": result, "exchange_code": str(exc.code),
                              "exchange_message": str(exc)[:200]})
        return outcome
    except Exception as exc:  # decrypt failure, bug in a client: still record it
        outcome = Verification(verified=False, result="error",
                               detail=f"verification failed: {type(exc).__name__}", error_code=type(exc).__name__)
        _record(key, outcome, now)
        await db.commit()
        logger.exception("api_key_verification_crashed", extra=log_ctx)
        return outcome

    can_trade = info.get("can_trade")
    can_withdraw = info.get("can_withdraw")
    permissions = list(info.get("permissions") or [])
    extra = list(info.get("extra") or [])
    if info.get("read_only"):
        outcome = Verification(verified=False, result="read_only_key", detail=READ_ONLY_MESSAGE,
                               withdrawal_enabled=can_withdraw, permissions=permissions, extra_permissions=extra)
        _record(key, outcome, now)
        await db.commit()
        logger.warning("api_key_verification_failed", extra={**log_ctx, "result": outcome.result, "permissions": permissions})
        return outcome
    if can_trade is False:
        outcome = Verification(verified=False, result="no_trade_permission",
                               detail=f"Spot trading permission is required. {venue} accepted the key but it "
                                      f"{'has no Spot -> Trade permission' if key.exchange == 'bitget' else 'cannot trade spot'} "
                                      f"(permissions: {', '.join(permissions) or 'none'}). Enable spot trading on the key.",
                               withdrawal_enabled=can_withdraw, permissions=permissions, extra_permissions=extra)
        _record(key, outcome, now)
        await db.commit()
        logger.warning("api_key_verification_failed",
                       extra={**log_ctx, "result": outcome.result, "permissions": info.get("permissions")})
        return outcome

    perms = ", ".join(info.get("permissions") or [])
    unconfirmed = ", ".join(info.get("unconfirmed") or [])
    possible = (f"WARNING: treat this key as able to withdraw. {venue} reports permission codes this platform "
                f"cannot confirm ({unconfirmed or perms or 'none reported'}); use it for accumulate purchases only "
                "unless you have checked on the exchange that withdrawals are disabled.")
    if can_trade is True:
        detail = f"Key verified on {venue}: spot trading enabled"
        if can_withdraw:
            detail += ". WARNING: this key can also withdraw — restrict it to spot trading only."
        elif can_withdraw is None:
            detail += ". " + possible
    elif perms:
        detail = (f"Key verified on {venue}: it authenticates, but its permissions are reported as codes this "
                  f"platform cannot confirm ({perms}); trade permission will be confirmed by the first order. " + possible)
    else:
        detail = (f"Key verified on {venue}; {venue} does not report the key's permissions, so treat it as able "
                  "to withdraw unless you have disabled that on the exchange")
    if extra:
        detail += (". This key has more permissions than AutoBuy needs (" + ", ".join(extra) + "). "
                   "For safety, use a key with only Spot -> Trade.")
    outcome = Verification(verified=True, result="ok", detail=detail, withdrawal_enabled=can_withdraw,
                           permissions=permissions, extra_permissions=extra)
    _record(key, outcome, now)
    await db.commit()
    logger.info("api_key_verified", extra={**log_ctx, "withdrawal_enabled": can_withdraw,
                                           "permissions": info.get("permissions")})
    return outcome


def _record(key: APIKey, outcome: Verification, now: datetime) -> None:
    key.verified = outcome.verified
    key.verification_status = "verified" if outcome.verified else "failed"
    key.verification_error = None if outcome.verified else outcome.detail[:500]
    key.last_checked_at = now
    key.verified_at = now if outcome.verified else key.verified_at
    key.withdrawal_enabled = outcome.withdrawal_enabled
    key.permissions = ",".join(outcome.permissions)[:500] or None
    key.updated_at = now
