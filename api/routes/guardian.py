"""Guardian, the customer's side of it: their monitored exchange accounts (v0.9.2).

Guardian watches a customer's exchange account through a SEPARATE read-only
key and tells them when something changes. It never trades, withdraws or
transfers, and nothing in this module can: the only exchange-facing code it
reaches is the engine's read-only client, and the only tables it writes are
the engine's. The engine (`guardian/`) owns every rule about an account; these
routes check who is asking, hand the request to the engine through
`api/services/guardian_bridge.py` and map the answer to HTTP.

Every route hangs off two router dependencies, in this order:
`require_guardian_flag` answers 404 "Not found" to everyone, signed in or not,
while the feature is off, before any credential is looked at; then
`require_guardian_enabled` needs the signed-in user and answers the same 404
when the caller is not on the allowlist. An account that is not the caller's
is 404 on every path for the same reason. Responses are built from
`GuardianAccountOut`, which has no field for key material or for the key
fingerprint.

Engine calls go through `bridge.run_db`, which hands the engine its database
on the bridge thread (never from the event loop), and the engine records its
operator pages (a replacement key for a different account, no recorded account
id to check one against, a partial or failed baseline) through
`bridge.operator_pager()`: when a refusal tells the customer Minas has been
told, a `guardian_operator_pages` row says so.

Adding an account verifies the key against the exchange's own permission
report during the request and takes the account's first snapshot (its
"baseline") in a background task, so the customer does not wait on several
exchange calls. The account shows `baseline_status: seeding` until that is
done, and the engine does not check it before then.

The engine modules that carry the exchange adapters (`guardian.onboarding`,
`guardian.keyreplacement`, ccxt underneath) are imported inside the handlers
that need them: an API process with the feature off never loads them.

No `from __future__ import annotations` here: slowapi's wrapper resolves the
endpoint annotations against its own module, so they must be real objects.
"""
import hashlib
import hmac
import logging
from typing import List

import psycopg2
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import APIKey, User
from api.schemas.guardian import (
    GuardianAccountCreate,
    GuardianAccountOut,
    GuardianAccountUpdate,
    GuardianKeyReplace,
    GuardianKeyReplaceResult,
    PASSPHRASE_REQUIRED,
    account_out,
)
from api.services import guardian_bridge as bridge
from api.services.crypto import InvalidToken, decrypt_value
from api.services.guardian_access import NOT_FOUND, require_guardian_enabled, require_guardian_flag
from api.services.rate_limit import limiter, user_or_ip_key
from guardian import accounts as engine_accounts
from guardian.crypto import redact_secrets

logger = logging.getLogger("autobuy.guardian")

router = APIRouter(dependencies=[Depends(require_guardian_flag), Depends(require_guardian_enabled)])

MAX_ACCOUNTS = 10

# Customer-facing refusals: what happened and what to do, nothing technical.
TOO_MANY_ACCOUNTS = "You already have ten monitored accounts. Remove one before adding another."
PURCHASE_KEY_REFUSED = (
    "That looks like one of your AutoBuy purchase keys. Guardian needs a separate read-only key: "
    "create one on the exchange with read permissions only."
)
KEY_ALREADY_MONITORED = (
    "That key is already being used to watch an account, so nothing was added. If you switched that "
    "account off, switch it back on instead; otherwise create a fresh read-only key."
)
NOTHING_TO_UPDATE = "Send at least one field to change."

_FINGERPRINT_CONSTRAINT = "uq_guardian_accounts_key_fingerprint"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _actor(user: User) -> str:
    """Who did it, as the engine's append-only events record it. The id, not
    the address: the events are kept for years and read on the customer's
    activity page, and operator pages leave the system."""
    return f"user:{user.id}"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


async def _own_account(user: User, account_id: int) -> dict:
    """The caller's account row, or 404. Not theirs, removed and non-existent
    are one answer on purpose: nobody learns whether an id exists."""
    row = await bridge.run_db_read(engine_accounts.account_for_user, user.id, account_id)
    if row is None:
        raise _not_found()
    return row


async def _is_purchase_key(db: AsyncSession, user: User, api_key: str) -> bool:
    """Is this one of the caller's AutoBuy purchase keys? Compared as sha256
    digests of the plaintexts, row by row, so the submitted key is never
    logged or stored. A row that does not decrypt (another cipher key) is
    skipped: it cannot be compared, and it is not this key."""
    offered = hashlib.sha256(api_key.encode()).hexdigest()
    tokens = (await db.execute(select(APIKey.encrypted_api_key).where(APIKey.user_id == user.id))).scalars().all()
    for token in tokens:
        try:
            stored = hashlib.sha256(decrypt_value(token).encode()).hexdigest()
        except (InvalidToken, TypeError, ValueError):
            continue
        if hmac.compare_digest(stored, offered):
            return True
    return False


def _is_fingerprint_collision(exc: psycopg2.IntegrityError) -> bool:
    return getattr(getattr(exc, "diag", None), "constraint_name", None) == _FINGERPRINT_CONSTRAINT


async def _seed_in_background(account_id: int, actor: str) -> None:
    """The second half of adding an account: the baseline, taken after the
    201 went out, on the bridge's own seeding thread so a slow exchange never
    holds a request thread. The engine marks the row 'complete' or
    'incomplete', never leaves it 'seeding', and pages Minas about a partial
    or failed seed through the bridge's operator pager; this only keeps the
    log honest, with the account id and nothing else."""
    from guardian.onboarding import seed_account_baseline

    try:
        outcome = await bridge.run_db_background(seed_account_baseline, bridge.cipher(), account_id, actor=actor,
                                                 operator_alerter=bridge.operator_pager())
    except Exception as exc:  # noqa: BLE001 - the row's state is the engine's job; the log line is ours
        logger.error("guardian account %s: baseline seeding raised %s", account_id, type(exc).__name__)
        return
    logger.info("guardian account %s: baseline %s", account_id, outcome)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/accounts", response_model=GuardianAccountOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/minute", key_func=user_or_ip_key)
async def add_account(
    request: Request,
    response: Response,
    payload: GuardianAccountCreate,
    background: BackgroundTasks,
    current_user: User = Depends(require_guardian_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Start watching an exchange account with a separate read-only key.

    The key is checked against the exchange's own permission report before
    anything is stored. A Bitget or OKX key without its passphrase is refused
    as a body error (422) with a plain message before the exchange is asked.
    Refused with 400 and a plain message: an eleventh account, one of the
    caller's AutoBuy purchase keys, and a key that can trade, withdraw or
    transfer (or cannot read the account). Refused with 409: an exchange account this user already watches (the message names the
    existing account). On success the account is stored with its first
    snapshot still being taken (`baseline_status: seeding`), which finishes in
    the background; the response never carries key material. 3 a minute."""
    from guardian.onboarding import AlreadyMonitored, OnboardingRejected, generate_unique_label, onboard_account

    if await bridge.run_db(engine_accounts.count_live_accounts, current_user.id) >= MAX_ACCOUNTS:
        raise HTTPException(status_code=400, detail=TOO_MANY_ACCOUNTS)
    if await _is_purchase_key(db, current_user, payload.api_key):
        logger.info("guardian: a purchase key was offered as a monitoring key and refused",
                    extra={"user_id": str(current_user.id)})
        raise HTTPException(status_code=400, detail=PURCHASE_KEY_REFUSED)
    actor = _actor(current_user)
    label = await bridge.run_db(generate_unique_label, payload.name)
    try:
        result = await bridge.run_db(
            onboard_account, bridge.cipher(),
            label=label, topology=payload.topology, api_key=payload.api_key, secret=payload.api_secret,
            passphrase=payload.passphrase or "", exchange=payload.exchange, user_id=current_user.id, source="self",
            actor=actor, customer_name=payload.name, alert_phone=payload.alert_phone,
            alert_email_secondary=payload.alert_email_secondary, alert_phone_secondary=payload.alert_phone_secondary,
            telegram_handle=payload.telegram_handle, sms_alerts_enabled=payload.sms_alerts_enabled, seed=False,
            operator_alerter=bridge.operator_pager(),
        )
    except AlreadyMonitored as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except OnboardingRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except psycopg2.IntegrityError as exc:
        # The same key on a row the duplicate check skips (switched off, or another owner's):
        # the unique fingerprint refuses it, and the customer gets a sentence, not a constraint name.
        if not _is_fingerprint_collision(exc):
            raise
        raise HTTPException(status_code=409, detail=KEY_ALREADY_MONITORED)
    logger.info("guardian account %s added; baseline seeding scheduled", result.account_id,
                extra={"user_id": str(current_user.id)})
    background.add_task(_seed_in_background, result.account_id, actor)
    return account_out(await _own_account(current_user, result.account_id))


@router.get("/accounts", response_model=List[GuardianAccountOut])
async def list_accounts(current_user: User = Depends(require_guardian_enabled)):
    """The caller's monitored accounts, oldest first, each with the engine's
    last successful check (`last_success_at`, UTC, null until the first).
    Removed accounts are not listed."""
    rows = await bridge.run_db_read(engine_accounts.accounts_for_user, current_user.id)
    return [account_out(row) for row in rows]


@router.get("/accounts/{account_id}", response_model=GuardianAccountOut)
async def get_account(account_id: int, current_user: User = Depends(require_guardian_enabled)):
    """One of the caller's monitored accounts. 404 for anyone else's, for a
    removed one and for an unknown id alike."""
    return account_out(await _own_account(current_user, account_id))


@router.put("/accounts/{account_id}/key", response_model=GuardianKeyReplaceResult)
@limiter.limit("3/minute", key_func=user_or_ip_key)
async def replace_key(
    request: Request,
    response: Response,
    account_id: int,
    payload: GuardianKeyReplace,
    current_user: User = Depends(require_guardian_enabled),
    db: AsyncSession = Depends(get_db),
):
    """Replace the monitoring key of one of the caller's accounts.

    The new key must be read-only AND belong to the same exchange account as
    the old one (a valid read-only key for a different account would leave
    the real one unwatched); a Bitget or OKX account needs the new key's
    passphrase too, and one of the caller's AutoBuy purchase keys is refused,
    both before the exchange is asked. A refusal is 400 with a plain message
    and nothing is changed; the refusals the engine pages Minas about (a key
    for a different account, no recorded account id to check one against)
    are recorded. On success monitoring continues with the new key on the
    next check and the change is recorded on the account's activity. 3 a
    minute."""
    from guardian.keyreplacement import replace_monitoring_key

    account = await _own_account(current_user, account_id)
    if payload.missing_passphrase_for(account["exchange"]):
        raise HTTPException(status_code=400, detail=PASSPHRASE_REQUIRED)
    if await _is_purchase_key(db, current_user, payload.api_key):
        logger.info("guardian account %s: a purchase key was offered as the replacement key and refused", account_id)
        raise HTTPException(status_code=400, detail=PURCHASE_KEY_REFUSED)
    outcome = await bridge.run_db(
        replace_monitoring_key, bridge.cipher(),
        account_id=account_id, api_key=payload.api_key, secret=payload.api_secret,
        passphrase=payload.passphrase or "", actor=_actor(current_user), operator_alerter=bridge.operator_pager(),
    )
    if not outcome.ok:
        # The engine's detail names the check that failed and what the exchange answered. It is
        # written for this line, never for the customer; the key material is scrubbed from it here
        # as well, so no exchange error text can carry it into the log.
        logger.warning("guardian account %s: replacement key refused (%s): %s", account_id, outcome.reason_code,
                       redact_secrets(outcome.detail, payload.api_key, payload.api_secret, payload.passphrase))
        raise HTTPException(status_code=400, detail=outcome.message)
    logger.info("guardian account %s: monitoring key replaced", account_id)
    return GuardianKeyReplaceResult(ok=True, message=outcome.message)


@router.patch("/accounts/{account_id}", response_model=GuardianAccountOut)
async def update_account(
    account_id: int,
    payload: GuardianAccountUpdate,
    current_user: User = Depends(require_guardian_enabled),
):
    """Change where this account's alerts go (`alert_phone`,
    `alert_email_secondary`, `alert_phone_secondary`, `telegram_handle`; null
    clears a field) and whether SMS alerts are sent (`sms_alerts_enabled`).
    The account owner's sign-in email always stays the primary destination.
    The change is recorded on the account's activity by field name only."""
    await _own_account(current_user, account_id)
    changes = payload.changes()
    if not changes:
        raise HTTPException(status_code=400, detail=NOTHING_TO_UPDATE)
    try:
        row = await bridge.run_db(engine_accounts.update_contacts, account_id, actor=_actor(current_user), **changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return account_out(row)


@router.post("/accounts/{account_id}/disable", response_model=GuardianAccountOut)
async def disable_account(account_id: int, current_user: User = Depends(require_guardian_enabled)):
    """Switch monitoring off for this account. The engine stops checking it
    from the next pass; the key stays stored so it can be switched back on.
    Recorded on the account's activity."""
    await _own_account(current_user, account_id)
    try:
        row = await bridge.run_db(engine_accounts.set_status, account_id, "disabled", actor=_actor(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return account_out(row)


@router.post("/accounts/{account_id}/enable", response_model=GuardianAccountOut)
async def enable_account(account_id: int, current_user: User = Depends(require_guardian_enabled)):
    """Switch monitoring back on for this account. It is checked again from
    the engine's next pass, with its baseline as it was. Recorded on the
    account's activity."""
    await _own_account(current_user, account_id)
    try:
        row = await bridge.run_db(engine_accounts.set_status, account_id, "active", actor=_actor(current_user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return account_out(row)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_account(account_id: int, current_user: User = Depends(require_guardian_enabled)):
    """Remove this account: monitoring stops and the stored key material is
    scrubbed. The account's alert and event history is kept as its record
    (and the removal is added to it), but the account no longer appears in
    this API. The same key may be added again later as a new account."""
    await _own_account(current_user, account_id)
    await bridge.run_db(engine_accounts.scrub_and_delete, account_id, actor=_actor(current_user))
    logger.info("guardian account %s removed by its owner; key material scrubbed, history kept", account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
