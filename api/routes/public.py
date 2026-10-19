"""Unauthenticated, read-only facts the landing page and setup guide need:
the supported coins and exchanges, the support address and the server's
public IP that customers allowlist on their Bitget key, and whether Guardian
(read-only exchange monitoring) is switched on for this deployment: the raw
`GUARDIAN_ENABLED` flag only, for the sign-in page's hint; the allowlist is a
per-user decision made after login and is not readable here. Nothing else from
settings is exposed here. Static per deploy, so it is cacheable for 5 minutes
and rate limited per IP."""
from typing import List, Optional

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from api.config import settings
from api.services.rate_limit import limiter
from api.utils.exchange_networks import ACCUMULATE_ONLY, SUPPORTED_CRYPTOS, categories_with_coins

router = APIRouter()


class CoinEntry(BaseModel):
    symbol: str
    name: str


class CoinCategory(BaseModel):
    category: str          # main | stable | meme
    label: str             # descriptive only: "Main coins", "Stablecoins", "Meme coins"
    accumulate_only: bool  # bought and held on the exchange; withdraw mode never offered
    risk_note: Optional[str] = None
    coins: List[CoinEntry]


class PublicMeta(BaseModel):
    coins: List[str]
    coin_categories: List[CoinCategory] = []
    accumulate_only: List[str] = []
    exchanges: List[str]
    support_email: str
    server_ip: str
    guardian_enabled: bool = False   # the raw GUARDIAN_ENABLED flag (v0.9.2); the allowlist is not consulted


@router.get("/meta", response_model=PublicMeta)
@limiter.limit("60/minute")
async def public_meta(request: Request, response: Response):
    """Supported coins and exchanges, the support address, the server IP to
    allowlist on an exchange API key, and whether Guardian is switched on for
    this deployment (`guardian_enabled`, the raw flag). Anonymous, cacheable
    for 5 minutes."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return PublicMeta(coins=list(SUPPORTED_CRYPTOS), coin_categories=[CoinCategory(**c) for c in categories_with_coins()],
                      accumulate_only=sorted(ACCUMULATE_ONLY), exchanges=list(settings.supported_exchanges),
                      support_email=settings.support_email or "", server_ip=settings.public_server_ip or "",
                      guardian_enabled=bool(settings.guardian_enabled))
