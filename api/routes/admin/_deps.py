"""Shared by the admin route modules: the role guard and the audit log line."""
import logging

from fastapi import Depends, HTTPException

from api.models import User
from api.routes.user import get_current_user

logger = logging.getLogger("autobuy.admin")

VALID_KYC = {"pending", "verified", "rejected"}


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def audit(admin: User, action: str, **details) -> None:
    logger.info("admin_action", extra={"admin_id": str(admin.id),
                                       "action": action, **details})
