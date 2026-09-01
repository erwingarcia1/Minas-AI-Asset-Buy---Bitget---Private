"""TEMPORARY manual-execution routes for exercising the purchase executor.

Scheduled for removal in Phase 3 once the scheduler and test suite cover
execution end-to-end. Dry-run is the default; live orders require an explicit
dry_run=false.
"""
import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import APIKey, User
from api.routes.exchange import decrypt_value
from api.routes.transactions import TransactionResponse
from api.routes.user import get_current_user
from fastapi import Request
from api.services.rate_limit import limiter, user_or_ip_key
from api.services.binance_client import BinanceClient, BinanceClientError
from api.services.purchase_service import PurchaseNotFound, PurchaseService

router = APIRouter()


@router.post("/execute/{purchase_id}", response_model=TransactionResponse)
@limiter.limit("1/minute", key_func=user_or_ip_key)
async def execute_purchase(
    request: Request,
    purchase_id: UUID,
    dry_run: bool = Query(True),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute one of the caller's recurring purchases right now."""
    try:
        result = await PurchaseService(db).execute_purchase(
            current_user.id, purchase_id, dry_run=dry_run)
    except PurchaseNotFound:
        raise HTTPException(status_code=404, detail="Recurring purchase not found")
    from api.services.purchase_service import PurchaseSkipped
    if isinstance(result, PurchaseSkipped):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200,
                            content={"skipped": True, "reason": result.reason})
    return result


@router.get("/balance/{coin}")
async def account_balance(
    coin: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Available spot balance of `coin` on the caller's Binance account."""
    key = (await db.execute(
        select(APIKey).where(APIKey.user_id == current_user.id,
                             APIKey.exchange == "binance"))).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="No Binance API key on file")
    client = BinanceClient(decrypt_value(key.encrypted_api_key),
                           decrypt_value(key.encrypted_api_secret))
    try:
        balance = await asyncio.to_thread(client.get_account_balance, coin)
    except BinanceClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"symbol": coin.upper(), "balance": balance}
