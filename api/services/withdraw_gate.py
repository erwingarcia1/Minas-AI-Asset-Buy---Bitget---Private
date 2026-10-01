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


def is_globally_enabled() -> bool:
    return str(settings.withdrawals_enabled or "").strip().lower() in ("1", "true", "yes", "on")


def withdrawals_allowed(owner) -> bool:
    """True when withdraw-immediately purchases may be created/switched/re-armed for this owner."""
    if is_globally_enabled():
        return True
    email = (getattr(owner, "email", None) or "").strip().lower()
    return bool(email) and email in settings.withdraw_allowed_emails


def require_allowed(owner) -> None:
    """403 with the customer-facing message when the owner is not allowed."""
    if not withdrawals_allowed(owner):
        raise HTTPException(status_code=403, detail={"error": "withdrawals_disabled", "details": {"message": MESSAGE}})


def require_owner_allowed_for_admin(owner) -> None:
    """403 for admin re-arm/resolve paths: the OWNER must be allowed, not the admin."""
    if not withdrawals_allowed(owner):
        raise HTTPException(status_code=403, detail={"error": "withdrawals_disabled", "details": {"message": ADMIN_MESSAGE}})
