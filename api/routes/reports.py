"""Tax and performance reports with CSV/PDF export."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.models import User
from api.routes.user import get_current_user
from api.services import report_generator as gen
import api.services.market_data as market_mod

router = APIRouter()


@router.get("/tax-summary")
async def tax_summary(
    year: int = Query(default=None, ge=2020, le=2100),
    format: str = Query("json", pattern="^(json|csv)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """FIFO acquisition summary for a calendar year (defaults to the current
    one). `format=csv` streams a spreadsheet-ready lot list."""
    year = year or datetime.now(timezone.utc).year
    summary = await gen.tax_summary(db, current_user.id, year)
    if format == "csv":
        return Response(
            content=gen.tax_summary_csv(summary), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="autobuy-tax-{year}.csv"'})
    return summary


@router.get("/performance")
async def performance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """ROI (total and annualized), spend volatility, and buy-cadence facts.
    Degrades to cost-basis-only when live prices are unavailable."""
    return await gen.performance(db, current_user.id, market_mod.market_data)


@router.get("/export")
async def export_pdf(
    year: int = Query(default=None, ge=2020, le=2100),
    format: str = Query("pdf", pattern="^pdf$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """PDF acquisition report for the year (accountant-friendly)."""
    year = year or datetime.now(timezone.utc).year
    summary = await gen.tax_summary(db, current_user.id, year)
    if not summary["acquisitions"]:
        raise HTTPException(status_code=404,
                            detail=f"No completed purchases in {year}")
    pdf = gen.tax_summary_pdf(summary, current_user.email)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="autobuy-report-{year}.pdf"'})
