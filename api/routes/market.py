"""Bitget market data for the purchase form: the coin's USDT pair and the
chains it can be withdrawn on, with fee, minimum and the smallest purchase
that still clears the minimum withdrawal. Mounted at /api/v2/market."""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.models import User
from api.routes.user import get_current_user
from api.services import bitget_market
from api.utils.chains import DEFAULT_CHAIN_FOR_COIN, FAMILY_LABELS, family_for_chain
from api.utils.exchange_networks import SUPPORTED_CRYPTOS

router = APIRouter()
logger = logging.getLogger("autobuy.market")


class ChainOut(BaseModel):
    chain: str
    family: Optional[str] = None          # bitcoin | ethereum | solana | tron | None (not supported here)
    family_label: Optional[str] = None
    supported: bool                        # this platform can validate addresses for it
    withdrawable: bool
    withdraw_fee: float
    extra_withdraw_fee_pct: float
    min_withdraw_amount: float
    withdraw_min_scale: int
    withdraw_confirm: int
    need_tag: bool
    browser_url: Optional[str] = None
    is_default: bool = False
    # priced at the live ask when amount_usd is given
    fee_usd: Optional[float] = None
    min_purchase_usd: Optional[float] = None
    estimated_sent: Optional[float] = None       # coins handed to Bitget for the withdrawal
    estimated_received: Optional[float] = None   # coins the wallet should receive
    meets_minimum: Optional[bool] = None


class CoinMarketOut(BaseModel):
    coin: str
    pair: str
    online: bool
    min_trade_usdt: float
    taker_fee_rate: Optional[float] = None
    quantity_precision: int
    price_ask: Optional[float] = None
    chains: List[ChainOut]
    fee_mode: str = "exclusive"   # BITGET_WITHDRAW_FEE_MODE: exclusive = fee on top of the sent amount
    note: str = "Fees and minimums are Bitget's live values, cached for up to an hour."


@router.get("/coins/{coin}", response_model=CoinMarketOut)
async def coin_market(coin: str, amount_usd: Optional[float] = Query(None, ge=1),
                      current_user: User = Depends(get_current_user)):
    """Bitget pair + withdrawal chains for a coin; with `amount_usd`, each chain
    also reports the fee in USD, what the wallet would receive and whether the
    amount clears the minimum withdrawal."""
    coin = coin.upper()
    if coin not in SUPPORTED_CRYPTOS:
        raise HTTPException(status_code=404, detail=f"unsupported coin {coin}")
    try:
        symbol = await bitget_market.get_symbol(coin)
        chains = await bitget_market.get_chains(coin)
        price = await asyncio.to_thread(bitget_market.live_ask, coin) if amount_usd else None
    except bitget_market.MarketDataUnavailable as exc:
        logger.warning("market data unavailable for %s: %s", coin, exc)
        raise HTTPException(status_code=503, detail={"error": "exchange_data_unavailable",
                                                    "details": {"message": bitget_market.UNAVAILABLE_MESSAGE}})
    if symbol is None:
        raise HTTPException(status_code=404, detail={"error": "pair_not_available",
                                                    "details": {"coin": coin, "message": f"{coin}/USDT is not a Bitget spot pair"}})
    out = []
    for c in chains:
        fam = family_for_chain(c["chain"])
        row = ChainOut(chain=c["chain"], family=fam, family_label=FAMILY_LABELS.get(fam) if fam else None,
                       supported=fam is not None, withdrawable=c["withdrawable"], withdraw_fee=c["withdraw_fee"],
                       extra_withdraw_fee_pct=c["extra_withdraw_fee_pct"], min_withdraw_amount=c["min_withdraw_amount"],
                       withdraw_min_scale=c["withdraw_min_scale"], withdraw_confirm=c["withdraw_confirm"],
                       need_tag=c["need_tag"], browser_url=c["browser_url"],
                       is_default=(c["chain"] or "").lower() == DEFAULT_CHAIN_FOR_COIN.get(coin, "").lower())
        if price and amount_usd:
            est = bitget_market.estimate_received(amount_usd, price, symbol, c)
            row.fee_usd = round(est["withdraw_fee"] * price, 4)
            row.min_purchase_usd = bitget_market.minimum_purchase_usd(price, symbol, c)
            row.estimated_sent = round(est["withdraw_size"], 8)
            row.estimated_received = round(est["received"], 8)
            row.meets_minimum = bitget_market.meets_minimum(est)
        out.append(row)
    return CoinMarketOut(coin=coin, pair=symbol["symbol"], online=symbol["online"], min_trade_usdt=symbol["min_trade_usdt"],
                         taker_fee_rate=symbol.get("taker_fee_rate"), quantity_precision=symbol["quantity_precision"],
                         price_ask=price, chains=out, fee_mode=bitget_market.fee_mode())
