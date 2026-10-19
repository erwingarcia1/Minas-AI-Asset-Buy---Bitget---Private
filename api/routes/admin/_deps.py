"""Shared by the admin route modules: the role guard and the audit log line."""
import logging

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.models import User
from api.routes.user import get_current_user

logger = logging.getLogger("autobuy.admin")

VALID_KYC = {"pending", "verified", "rejected"}

ADMIN_MFA_MESSAGE = ("Two-factor authentication is required for admin accounts. "
                     "Set it up under Settings > Security, then sign in again.")


async def require_admin(current_user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)) -> User:
    """role=admin, and (when ADMIN_MFA_REQUIRED is on) a confirmed two-factor
    enrolment. The 403 for a missing second factor carries the error code
    `mfa_required_for_admin` so the UI can send the admin to enrol; the
    /api/user/2fa routes themselves need only a signed-in user."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if settings.admin_mfa_required:
        from api.services import twofactor
        if not await twofactor.is_enabled(db, current_user.id):
            raise HTTPException(status_code=403, detail={"error": "mfa_required_for_admin",
                                                         "details": {"message": ADMIN_MFA_MESSAGE}})
    return current_user


def audit(admin: User, action: str, **details) -> None:
    logger.info("admin_action", extra={"admin_id": str(admin.id),
                                       "action": action, **details})
