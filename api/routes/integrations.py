"""Integration management: outbound webhook endpoints (CRUD + ping) and the
availability listing for connected services."""
import secrets as pysecrets
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.models import APIKey, User, WebhookEndpoint
from api.routes.user import UTCModel, get_current_user
from api.services.webhook_service import deliver_one

router = APIRouter()


class IntegrationInfo(BaseModel):
    name: str
    kind: str          # exchange | alerting | automation
    status: str        # connected | available
    detail: str


@router.get("/available", response_model=List[IntegrationInfo])
async def available(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Everything the account can connect, with its current status."""
    keys = {k.exchange for k in (await db.execute(
        select(APIKey).where(APIKey.user_id == current_user.id))).scalars().all()}
    webhook_count = (await db.execute(
        select(func.count(WebhookEndpoint.id)).where(
            WebhookEndpoint.user_id == current_user.id,
            WebhookEndpoint.enabled.is_(True)))).scalar_one()
    items = []
    for venue in ("binance", "bitget"):
        items.append(IntegrationInfo(
            name=venue, kind="exchange",
            status="connected" if venue in keys else "available",
            detail="Trading key stored" if venue in keys
                   else "Add an API key under Exchange settings"))
    items.append(IntegrationInfo(
        name="webhooks", kind="automation",
        status="connected" if webhook_count else "available",
        detail=f"{webhook_count} active endpoint(s)" if webhook_count
               else "Push purchase events to your own automation"))
    items.append(IntegrationInfo(
        name="slack", kind="alerting",
        status="connected" if settings.slack_webhook_url else "available",
        detail="Operational alerts (server-side SLACK_WEBHOOK_URL)"))
    return items


class WebhookCreate(BaseModel):
    url: str = Field(max_length=500)

    @field_validator("url")
    @classmethod
    def _https(cls, value):
        if not value.startswith("https://"):
            raise ValueError("webhook URLs must be https://")
        return value


class WebhookRow(UTCModel):
    id: UUID
    url: str
    enabled: bool
    last_status: Optional[str] = None
    created_at: datetime


class WebhookCreated(WebhookRow):
    secret: str  # shown exactly once


@router.get("/webhooks", response_model=List[WebhookRow])
async def list_webhooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The caller's outbound endpoints (secrets are never returned again)."""
    rows = (await db.execute(select(WebhookEndpoint).where(
        WebhookEndpoint.user_id == current_user.id)
        .order_by(WebhookEndpoint.created_at))).scalars().all()
    return [WebhookRow.model_validate(r) for r in rows]


@router.post("/webhooks", response_model=WebhookCreated, status_code=201)
async def create_webhook(
    payload: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register an endpoint. The signing secret is returned ONCE — store it;
    verify deliveries via X-AutoBuy-Signature (hex HMAC-SHA256 of the body)."""
    count = (await db.execute(select(func.count(WebhookEndpoint.id)).where(
        WebhookEndpoint.user_id == current_user.id))).scalar_one()
    if count >= 5:
        raise HTTPException(status_code=422, detail="Maximum 5 webhook endpoints")
    ep = WebhookEndpoint(user_id=current_user.id, url=payload.url,
                         secret=pysecrets.token_hex(32))
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    out = WebhookCreated.model_validate(ep)
    out.secret = ep.secret
    return out


async def _owned(db, user, webhook_id) -> WebhookEndpoint:
    ep = (await db.execute(select(WebhookEndpoint).where(
        WebhookEndpoint.id == webhook_id,
        WebhookEndpoint.user_id == user.id))).scalar_one_or_none()
    if ep is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return ep


@router.post("/webhooks/{webhook_id}/ping", response_model=WebhookRow)
async def ping_webhook(
    webhook_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a signed test event and record the delivery result."""
    ep = await _owned(db, current_user, webhook_id)
    ep.last_status = await deliver_one(ep, {"event": "ping", "data": {"ok": True}})
    await db.commit()
    await db.refresh(ep)
    return WebhookRow.model_validate(ep)


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove an endpoint permanently."""
    ep = await _owned(db, current_user, webhook_id)
    await db.delete(ep)
    await db.commit()
