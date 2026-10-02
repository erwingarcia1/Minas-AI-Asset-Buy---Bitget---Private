"""Unauthenticated, read-only facts the landing page and setup guide need:
the supported coins and exchanges, the support address and the server's
public IP that customers allowlist on their Bitget key. Nothing else from
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


@router.get("/meta", response_model=PublicMeta)
@limiter.limit("60/minute")
async def public_meta(request: Request, response: Response):
    """Supported coins and exchanges, the support address and the server IP to
    allowlist on an exchange API key. Anonymous, cacheable for 5 minutes."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return PublicMeta(coins=list(SUPPORTED_CRYPTOS), coin_categories=[CoinCategory(**c) for c in categories_with_coins()],
                      accumulate_only=sorted(ACCUMULATE_ONLY), exchanges=list(settings.supported_exchanges),
                      support_email=settings.support_email or "", server_ip=settings.public_server_ip or "")
