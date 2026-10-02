"""Private-beta gate on withdraw-immediately ("autobuy") purchases.

No real withdrawal has ever run on the platform, so the paths that create or
switch a purchase into withdraw mode, retry a withdrawal, or re-arm a
withdraw-mode purchase are refused unless withdrawals are enabled globally or
the purchase OWNER's email is allowlisted. The check is always against the
owner, never the acting admin. The executor and the withdrawal code are
untouched: this is a gate at the API edges only.
"""
from fastapi import HTTPException

from api.config import settings

MESSAGE = ("Sending coins to your own wallet isn't available during the private beta. "
           "Your coins stay in your Bitget sub-account.")
ADMIN_MESSAGE = ("This purchase is in withdraw mode and its owner isn't allowed withdrawals during the private beta. "
                 "Ask the owner to switch it to accumulate, or resolve it keeping the coins on the exchange.")
RESUME_HINT = "Switch this purchase to accumulate first, then resume it."


def coin_message(coin: str) -> str:
    return f"{coin} is bought and held on Bitget only; it can't be sent to a wallet."


def require_coin_withdrawable(coin: str) -> None:
    """403 for accumulate-only coins (a category property), whatever the owner's
    allowlist status or the global switch say. Checked before the owner gate."""
    from api.utils.exchange_networks import is_withdrawable
    coin = (coin or "").upper()
    if not is_withdrawable(coin):
        raise HTTPException(status_code=403, detail={"error": "withdrawals_disabled",
                                                     "details": {"message": coin_message(coin), "coin": coin, "accumulate_only": True}})
REACTIVATE_HINT = "Switch your withdraw-mode purchases to accumulate first."
ADMIN_REACTIVATE_MESSAGE = ("This account owns active withdraw-mode purchases and isn't allowed withdrawals during the "
                            "private beta; reactivating it would let them run. Ask the owner to switch them to accumulate first.")


def is_globally_enabled() -> bool:
    return str(settings.withdrawals_enabled or "").strip().lower() in ("1", "true", "yes", "on")


def withdrawals_allowed(owner) -> bool:
    """True when withdraw-immediately purchases may be created/switched/re-armed for this owner."""
    if is_globally_enabled():
        return True
    email = (getattr(owner, "email", None) or "").strip().lower()
    return bool(email) and email in settings.withdraw_allowed_emails


def require_allowed(owner, hint: str | None = None) -> None:
    """403 with the customer-facing message (plus an optional hint) when the owner is not allowed."""
    if not withdrawals_allowed(owner):
        message = f"{MESSAGE} {hint}" if hint else MESSAGE
        raise HTTPException(status_code=403, detail={"error": "withdrawals_disabled", "details": {"message": message}})


async def active_withdraw_mode_purchase_ids(db, owner, key_id=None) -> list:
    """Ids of the owner's ACTIVE withdraw-mode purchases (optionally on one key). These are the
    purchases that would run again when the account, its KYC or its key is reactivated. Empty
    when the owner is allowed, so callers can gate on a non-empty result."""
    if withdrawals_allowed(owner):
        return []
    from sqlalchemy import select
    from api.models import Purchase
    q = select(Purchase.id).where(Purchase.user_id == owner.id, Purchase.status == "active", Purchase.purchase_type == "autobuy")
    if key_id is not None:
        q = q.where(Purchase.exchange_account_id == key_id)
    return [str(i) for i in (await db.execute(q)).scalars().all()]


async def require_no_active_withdraw_mode(db, owner, key_id=None, admin: bool = False) -> None:
    """403 when reactivating the owner (unsuspend, KYC back to verified, key re-verified) would
    put a withdraw-mode purchase back in front of the scheduler. Judged by the owner."""
    ids = await active_withdraw_mode_purchase_ids(db, owner, key_id)
    if ids:
        message = ADMIN_REACTIVATE_MESSAGE if admin else f"{MESSAGE} {REACTIVATE_HINT}"
        raise HTTPException(status_code=403, detail={"error": "withdrawals_disabled",
                                                     "details": {"message": message, "purchase_ids": ids}})


def require_owner_allowed_for_admin(owner) -> None:
    """403 for admin re-arm/resolve paths: the OWNER must be allowed, not the admin."""
    if not withdrawals_allowed(owner):
        raise HTTPException(status_code=403, detail={"error": "withdrawals_disabled", "details": {"message": ADMIN_MESSAGE}})
