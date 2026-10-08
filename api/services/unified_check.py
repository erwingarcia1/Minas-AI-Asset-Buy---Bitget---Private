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

from api.models import APIKey
from api.services import key_verification as kv
from api.services import telegram
from api.services.email_service import email_service
from api.services.email_templates import render

logger = logging.getLogger("autobuy.unified_check")

SUBJECT = "Your Bitget account needs a change before AutoBuy can buy again"
JOB_HOUR_UTC = 6     # 06:20 UTC daily: outside every current buy window and before the European morning
JOB_MINUTE_UTC = 20


async def _email(key: APIKey) -> bool:
    from api.utils.links import build_app_link
    user = key.user
    html, text = render("unified_account", greeting="Hello,", message=kv.unified_message(),
                        settings_url=build_app_link("/settings"))
    return await email_service.queue(user.email, SUBJECT, html, text, template="unified_account",
                                     category="failure", user_id=user.id)


async def check_unified_accounts(db, client_factory=None) -> dict:
    """One pass over the verified Bitget keys. Returns counts."""
    out = {"checked": 0, "turned_unified": 0, "unreadable": 0, "emails": 0}
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
        now = datetime.now(timezone.utc)
        kv._record(key, kv.Verification(verified=False, result="unified_account", detail=kv.unified_message(),
                                        error_code="40085", permissions=[p for p in (key.permissions or "").split(",") if p]), now)
        out["turned_unified"] += 1
        emailed = False
        try:
            emailed = await _email(key)
        except Exception as exc:   # noqa: BLE001
            logger.error("unified-account email not queued for key %s: %s", key.id, exc)
        if emailed:
            out["emails"] += 1
        await telegram.send(
            f"⚠️ Bitget account turned Unified\n{telegram.mask_email(key.user.email)} · key {str(key.id)[:8]}… · "
            f"verified off, buys on it will not run · {'email queued' if emailed else 'no email'}",
            f"unified_account:{key.id}")
        logger.warning("api_key_turned_unified", extra={"user_id": str(key.user_id), "key_id": str(key.id)})
    await db.commit()
    logger.info("unified-account check: %s", out)
    return out
