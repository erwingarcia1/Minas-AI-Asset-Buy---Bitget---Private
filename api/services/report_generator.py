"""Report assembly: tax/acquisition summaries and performance metrics, with
CSV and PDF renderers. The platform only BUYS, so realized gains are always
zero today — the FIFO lot list exists so cost basis is audit-ready the day
sells arrive. Everything computes from completed transactions."""
import csv
import io
import statistics
from datetime import datetime, timezone

from sqlalchemy import func, select

from api.models import Transaction


async def tax_summary(db, user_id, year: int) -> dict:
    """Per-crypto acquisition summary for a calendar year (FIFO lot method)."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    rows = (await db.execute(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.status == "completed",
            Transaction.created_at >= start,
            Transaction.created_at < end)
        .order_by(Transaction.created_at))).scalars().all()

    lots: dict[str, list] = {}
    for t in rows:
        lots.setdefault(t.crypto, []).append({
            "date": t.created_at.date().isoformat(),
            "amount": t.amount_crypto,
            "cost_usdt": t.cost_usdt,
            "price": (t.exchange_rate
                      or (t.cost_usdt / t.amount_crypto if t.amount_crypto else 0)),
        })
    holdings = []
    for crypto, lot_list in sorted(lots.items()):
        amount = sum(l["amount"] for l in lot_list)
        cost = sum(l["cost_usdt"] for l in lot_list)
        holdings.append({
            "crypto": crypto, "lots": lot_list,
            "total_amount": round(amount, 8),
            "total_cost_usdt": round(cost, 2),
            "average_price_usdt": round(cost / amount, 8) if amount else 0.0,
        })
    return {
        "year": year, "method": "FIFO",
        "realized_gains_usdt": 0.0,  # buy-only platform: no disposals yet
        "note": ("No disposals occurred through AutoBuy; all positions are "
                 "unrealized. Lots are FIFO-ordered for future sales."),
        "acquisitions": holdings,
        "total_acquired_usdt": round(sum(h["total_cost_usdt"] for h in holdings), 2),
    }


async def performance(db, user_id, market) -> dict:
    """ROI and risk metrics from completed history + live prices."""
    rows = (await db.execute(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.status == "completed")
        .order_by(Transaction.created_at))).scalars().all()
    if not rows:
        return {"has_data": False}

    invested = sum(t.cost_usdt for t in rows)
    amounts: dict[str, float] = {}
    for t in rows:
        amounts[t.crypto] = amounts.get(t.crypto, 0.0) + t.amount_crypto

    value = 0.0
    priced = True
    for crypto, amount in amounts.items():
        price = await market.get_price(crypto)
        if price is None:
            priced = False
            break
        value += amount * price

    daily: dict[str, float] = {}
    for t in rows:
        key = t.created_at.date().isoformat()
        daily[key] = daily.get(key, 0.0) + t.cost_usdt
    best_day = max(daily, key=lambda d: daily[d])
    first = rows[0].created_at
    years_held = max((datetime.now(timezone.utc) - first).days, 1) / 365.25

    out = {
        "has_data": True, "price_available": priced,
        "total_invested_usdt": round(invested, 2),
        "first_purchase": first.date().isoformat(),
        "purchase_count": len(rows),
        "largest_buy_day": {"date": best_day, "usdt": round(daily[best_day], 2)},
        "spend_volatility_pct": round(
            statistics.pstdev(daily.values()) / statistics.mean(daily.values()) * 100, 1)
            if len(daily) > 1 else 0.0,
    }
    if priced:
        roi = (value - invested) / invested
        out.update({
            "current_value_usdt": round(value, 2),
            "roi_pct": round(roi * 100, 2),
            "annualized_roi_pct": round(((1 + roi) ** (1 / years_held) - 1) * 100, 2),
        })
    return out


def tax_summary_csv(summary: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["crypto", "acquired_date", "amount", "cost_usdt",
                     "price_usdt", "method"])
    for holding in summary["acquisitions"]:
        for lot in holding["lots"]:
            writer.writerow([holding["crypto"], lot["date"],
                             f"{lot['amount']:.8f}", f"{lot['cost_usdt']:.2f}",
                             f"{lot['price']:.2f}", summary["method"]])
    return buf.getvalue()


def tax_summary_pdf(summary: dict, email: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            title=f"AutoBuy tax summary {summary['year']}")
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"AutoBuy acquisition summary — {summary['year']}",
                  styles["Title"]),
        Paragraph(f"Account: {email} · Method: {summary['method']} · "
                  f"Realized gains: ${summary['realized_gains_usdt']:.2f}",
                  styles["Normal"]),
        Spacer(1, 0.5 * cm),
    ]
    data = [["Crypto", "Amount", "Cost (USDT)", "Avg price"]]
    for h in summary["acquisitions"]:
        data.append([h["crypto"], f"{h['total_amount']:.8f}",
                     f"{h['total_cost_usdt']:.2f}",
                     f"{h['average_price_usdt']:.2f}"])
    data.append(["TOTAL", "", f"{summary['total_acquired_usdt']:.2f}", ""])
    table = Table(data, colWidths=[3 * cm, 5 * cm, 4 * cm, 4 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0E6E62")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story += [table, Spacer(1, 0.5 * cm),
              Paragraph(summary["note"], styles["Italic"])]
    doc.build(story)
    return buf.getvalue()
