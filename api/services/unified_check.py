"""Daily read-only check of every verified Bitget key (v0.7.7).

Bitget migrates Classic accounts to Unified Trading mode in batches; a migrated
account answers every Classic (v2) call with 40085 and the next buy would park.
This pass makes the same account-info call verification makes, once a day, and
when a key has turned Unified it records that on the key exactly like a failed
verification (``verified`` off, the customer-facing message in
``verification_error``), emails the customer once, tells the owner on Telegram
once and lets the purchases list show a banner. The key is no longer verified
afterwards, so it is not checked again: one alert per key. No order endpoint is
touched; every other venue error is counted and left alone.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.models import APIKey, Purchase
from api.services import key_verification as kv
from api.services import telegram
from api.services.email_service import email_service
from api.services.email_templates import render

logger = logging.getLogger("autobuy.unified_check")

SUBJECT = "Your Bitget account needs a change before AutoBuy can buy again"
JOB_HOUR_UTC = 6     # 06:20 UTC daily: outside every current buy window and before the European morning
JOB_MINUTE_UTC = 20


async def _pause_purchases(db, key: APIKey) -> int:
    """The normal pause path (what the Pause button does: status -> paused, nothing else), for every
    active purchase on the key, so no attempt runs and fails on it. Returns how many were paused."""
    rows = (await db.execute(select(Purchase).where(Purchase.exchange_account_id == key.id, Purchase.status == "active"))).scalars().all()
    for p in rows:
        p.status = "paused"
    return len(rows)


async def _email(key: APIKey, paused: int) -> bool:
    from api.utils.links import build_app_link
    user = key.user
    html, text = render("unified_account", greeting="Hello,", message=kv.unified_message(), paused=paused,
                        settings_url=build_app_link("/settings"), purchases_url=build_app_link("/purchases"))
    return await email_service.queue(user.email, SUBJECT, html, text, template="unified_account",
                                     category="failure", user_id=user.id)


READY_SUBJECT = "Good news: AutoBuy now works with your Bitget account"
STEP_SUBJECT = "Good news: AutoBuy now works with Unified Bitget accounts (one small step)"
# Texts agreed with the owner on 2026-10-10. "ready": the key verified through v3. "step": the key trades but
# lacks Management -> Read, so the customer must tick it and press Verify before resuming.
READY_TEXT = "Good news: AutoBuy now works with your Bitget account. Press Resume on your purchases whenever you're ready."
STEP_TEXT = ("Good news: AutoBuy now works with Unified Bitget accounts. One small step: in Bitget, edit your API key and tick "
             "Unified Trading Account → Management → Read. Then in AutoBuy, open Settings → Exchange API keys and press Verify. "
             "After that, you can resume your purchases.")


async def _email_outcome(key: APIKey, kind: str) -> bool:
    from api.utils.links import build_app_link
    template = "unified_ready" if kind == "ready" else "unified_step"
    html, text = render(template, greeting="Hello,", message=READY_TEXT if kind == "ready" else STEP_TEXT,
                        purchases_url=build_app_link("/purchases"), settings_url=build_app_link("/settings"))
    return await email_service.queue(key.user.email, READY_SUBJECT if kind == "ready" else STEP_SUBJECT, html, text,
                                     template=template, category="confirmation", user_id=key.user.id)


# Re-verify scope: keys the v0.7.7 flagging or a Unified refusal left unverified
FLAGGED_PATTERNS = ("%Unified Trading mode%", "%Management → Read%")


async def reverify_flagged(db, client_factory=None, send_emails: bool = False) -> dict:
    """One-off after the v0.8.0 deploy: every key flagged as Unified is verified again (now through v3) and its
    mode stored. Emails go out only with ``send_emails`` (owner's call, after the live test): "ready" to keys that
    verified, "step" to keys that trade but lack Management -> Read. Nothing is resumed: the customer presses Resume."""
    out = {"flagged": 0, "verified": 0, "needs_management_read": 0, "still_failed": 0, "emails": 0, "emails_held": 0}
    from sqlalchemy import or_
    keys = (await db.execute(select(APIKey).options(selectinload(APIKey.user))
                             .where(APIKey.exchange == "bitget", APIKey.verified.is_(False),
                                    or_(*[APIKey.verification_error.like(p) for p in FLAGGED_PATTERNS])))).scalars().all()
    for key in keys:
        out["flagged"] += 1
        outcome = await kv.verify_api_key(db, key, client_factory=client_factory)
        if outcome.verified:
            out["verified"] += 1
            kind = "ready"
        elif outcome.result == "unified_management_missing":
            out["needs_management_read"] += 1
            kind = "step"
        else:
            out["still_failed"] += 1
            logger.warning("reverify after v0.8.0: key %s still fails: %s", key.id, outcome.result)
            continue
        if not send_emails:
            out["emails_held"] += 1
            continue
        try:
            if await _email_outcome(key, kind):
                out["emails"] += 1
        except Exception as exc:   # noqa: BLE001
            logger.error("%s email not queued for key %s: %s", kind, key.id, exc)
    await db.commit()
    if out["flagged"]:
        await telegram.send(f"✅ v0.8.0 re-verify: {out['flagged']} flagged key(s), {out['verified']} verified as Unified, "
                            f"{out['needs_management_read']} need Management → Read, {out['still_failed']} still failing, "
                            f"{out['emails']} email(s) queued, {out['emails_held']} held", "unified_reverify")
    logger.info("reverify flagged keys: %s", out)
    return out


async def check_unified_accounts(db, client_factory=None) -> dict:
    """One pass over the verified Bitget keys. Returns counts."""
    out = {"checked": 0, "turned_unified": 0, "switched": 0, "unreadable": 0, "emails": 0, "paused": 0}
    keys = (await db.execute(select(APIKey).options(selectinload(APIKey.user))
                             .where(APIKey.exchange == "bitget", APIKey.verified.is_(True)))).scalars().all()
    for key in keys:
        out["checked"] += 1
        try:
            await asyncio.to_thread(kv._check, kv._client_for(key, client_factory))
            continue   # still Classic (or any readable answer): nothing to do
        except kv.ExchangeClientError as exc:
            if str(exc.code) not in kv.UNIFIED_CODES.get("bitget", set()):
                out["unreadable"] += 1
                logger.warning("unified check could not read key %s: %s", key.id, telegram.scrub(str(exc)))
                continue
        except Exception as exc:   # noqa: BLE001
            out["unreadable"] += 1
            logger.exception("unified check crashed on key %s: %s", key.id, type(exc).__name__)
            continue
        # v0.8.0: AutoBuy speaks v3 now. Re-verify through the Unified client; when that works the key keeps
        # trading and nothing is paused or emailed. Only when v3 also refuses does the v0.7.7 flagging run.
        reverified = await kv.verify_api_key(db, key, client_factory=client_factory)
        if reverified.verified:
            out["switched"] += 1
            await telegram.send(
                f"ℹ️ Bitget account switched to Unified, AutoBuy follows\n{telegram.mask_email(key.user.email)} · key {str(key.id)[:8]}… "
                f"· verified through v3, purchases keep running", f"unified_switched:{key.id}")
            logger.info("api_key_switched_to_unified", extra={"user_id": str(key.user_id), "key_id": str(key.id)})
            continue
        now = datetime.now(timezone.utc)
        kv._record(key, kv.Verification(verified=False, result="unified_account", detail=kv.unified_message(),
                                        error_code="40085", permissions=[p for p in (key.permissions or "").split(",") if p]), now)
        out["turned_unified"] += 1
        paused = await _pause_purchases(db, key)   # v0.7.7.1: nothing runs and fails on a flagged key
        out["paused"] += paused
        emailed = False
        try:
            emailed = await _email(key, paused)
        except Exception as exc:   # noqa: BLE001
            logger.error("unified-account email not queued for key %s: %s", key.id, exc)
        if emailed:
            out["emails"] += 1
        await telegram.send(
            f"⚠️ Bitget account turned Unified\n{telegram.mask_email(key.user.email)} · key {str(key.id)[:8]}… · "
            f"verified off, {paused} active purchase(s) paused · {'email queued' if emailed else 'no email'}",
            f"unified_account:{key.id}")
        logger.warning("api_key_turned_unified", extra={"user_id": str(key.user_id), "key_id": str(key.id)})
    await db.commit()
    logger.info("unified-account check: %s", out)
    return out
