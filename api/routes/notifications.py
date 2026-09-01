"""Notification preferences, contact channels, history, and a test blast."""
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import Notification, User
from api.routes.user import UTCModel, get_current_user
from api.services.notifications import Preferences, dispatcher

router = APIRouter()


class PreferencesPayload(BaseModel):
    email_enabled: Optional[bool] = None
    sms_enabled: Optional[bool] = None
    push_enabled: Optional[bool] = None
    quiet_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(default=None, ge=0, le=23)
    phone_number: Optional[str] = Field(default=None, max_length=32)
    device_token: Optional[str] = Field(default=None, max_length=255)

    @field_validator("phone_number")
    @classmethod
    def _e164(cls, value):
        if value is not None and value != "":
            import re
            if not re.match(r"^\+[1-9]\d{6,14}$", value):
                raise ValueError("phone_number must be E.164, e.g. +14155550123")
        return value


class PreferencesResponse(BaseModel):
    email_enabled: bool
    sms_enabled: bool
    push_enabled: bool
    quiet_hours_start: int
    quiet_hours_end: int
    phone_number: Optional[str] = None
    has_device_token: bool


def _response(user: User) -> PreferencesResponse:
    prefs = Preferences.for_user(user)
    return PreferencesResponse(
        **prefs.__dict__, phone_number=user.phone_number,
        has_device_token=bool(user.device_token))


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(current_user: User = Depends(get_current_user)):
    """The caller's notification channel settings."""
    return _response(current_user)


@router.put("/preferences", response_model=PreferencesResponse)
async def update_preferences(
    payload: PreferencesPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update channel toggles, quiet hours (UTC), phone number, device token."""
    prefs = Preferences.for_user(current_user)
    for field in ("email_enabled", "sms_enabled", "push_enabled",
                  "quiet_hours_start", "quiet_hours_end"):
        value = getattr(payload, field)
        if value is not None:
            setattr(prefs, field, value)
    current_user.notification_preferences = prefs.to_json()
    if payload.phone_number is not None:
        current_user.phone_number = payload.phone_number or None
        current_user.phone_verified = False
    if payload.device_token is not None:
        current_user.device_token = payload.device_token or None
    await db.commit()
    await db.refresh(current_user)
    return _response(current_user)


class NotificationRow(UTCModel):
    id: UUID
    channel: str
    template: str
    subject: str
    status: str
    created_at: datetime


class HistoryResponse(BaseModel):
    items: List[NotificationRow]
    total: int


@router.get("/history", response_model=HistoryResponse)
async def history(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Audit log of the caller's notifications across all channels."""
    total = (await db.execute(select(func.count(Notification.id)).where(
        Notification.user_id == current_user.id))).scalar_one()
    rows = (await db.execute(
        select(Notification).where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return HistoryResponse(items=[NotificationRow.model_validate(r) for r in rows],
                           total=total)


@router.post("/test")
async def send_test(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a test notification through every enabled channel."""
    results = await dispatcher.notify(
        db, current_user, template="test",
        subject="AutoBuy test notification",
        body="This is a test across your enabled channels. All good!")
    return {"channels": results}
