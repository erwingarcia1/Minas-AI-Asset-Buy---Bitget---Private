"""Unauthenticated, read-only facts the landing page and setup guide need:
the supported coins and exchanges, the support address and the server's
public IP that customers allowlist on their Bitget key. Nothing else from
settings is exposed here. Static per deploy, so it is cacheable for 5 minutes
and rate limited per IP."""
from typing import List

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from api.config import settings
from api.services.rate_limit import limiter
from api.utils.exchange_networks import SUPPORTED_CRYPTOS

router = APIRouter()


class PublicMeta(BaseModel):
    coins: List[str]
    exchanges: List[str]
    support_email: str
    server_ip: str


@router.get("/meta", response_model=PublicMeta)
@limiter.limit("60/minute")
async def public_meta(request: Request, response: Response):
    """Supported coins and exchanges, the support address and the server IP to
    allowlist on an exchange API key. Anonymous, cacheable for 5 minutes."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return PublicMeta(coins=list(SUPPORTED_CRYPTOS), exchanges=list(settings.supported_exchanges),
                      support_email=settings.support_email or "", server_ip=settings.public_server_ip or "")
