"""Report assembly: tax/acquisition summaries and performance metrics, with
CSV and PDF renderers, over the Auto-Buy history (purchase_transactions via
:mod:`api.services.history`). The platform only BUYS, so realized gains are
always zero today — the FIFO lot list exists so cost basis is audit-ready the
day sells arrive. Everything computes from successful buys."""
import csv
import io
import statistics
from datetime import datetime, timezone

from api.services import history

CSV_COLUMNS = [
    "datetime_utc", "crypto", "quantity", "net_quantity", "fill_price_usd", "usd_amount",
    "fee", "fee_currency", "exchange", "order_id", "purchase_type", "tx_type",
    "withdrawal_address", "network", "withdrawal_fee", "withdrawal_fee_currency", "tx_hash",
    "withdrawal_status", "method",
]


def _lot(r: history.HistoryRow, wd: history.HistoryRow | None = None) -> dict:
    """One buy lot; when the buy was withdrawn (separate row), the withdrawal's
    address, chain, fee and tx hash are reported on the lot."""
    when = r.happened_at.astimezone(timezone.utc)
    withdrawal_status = None
    if wd is not None and wd.status in ("completed", "simulated"):
        withdrawal_status = wd.status
        r = history.HistoryRow(**{**r.__dict__, "withdrawal_id": wd.withdrawal_id, "wallet_address": wd.wallet_address,
                                  "blockchain_network": wd.blockchain_network, "withdrawal_fee": wd.withdrawal_fee,
                                  "withdrawal_fee_currency": wd.withdrawal_fee_currency, "tx_hash": wd.tx_hash,
                                  "tx_type": "withdrawal"})
    elif wd is not None:
        withdrawal_status = wd.status
    return {
        "datetime_utc": when.strftime("%Y-%m-%d %H:%M:%S"),
        "date": when.date().isoformat(),
        "amount": r.amount_crypto,          # gross fill (matches the exchange's order history)
        "net_amount": r.net_amount,         # the lot actually received, net of the coin-denominated fee
        "cost_usdt": r.cost_usd,
        "price": r.exchange_rate or 0.0,
        "fee": r.fee, "fee_currency": r.fee_currency,
        "exchange": r.exchange, "order_id": r.order_id,
        "purchase_type": r.purchase_type, "tx_type": r.tx_type,
        "withdrawal_address": r.wallet_address, "network": r.blockchain_network,
        "withdrawal_fee": r.withdrawal_fee, "withdrawal_fee_currency": r.withdrawal_fee_currency,
        "tx_hash": r.tx_hash,
        "withdrawal_status": withdrawal_status,
    }


async def tax_summary(db, user_id, year: int) -> dict:
    """Per-crypto acquisition summary for a calendar year (FIFO lot method)."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    rows = await history.completed_rows(db, user_id, since=start, until=end)
    withdrawals = await history.withdrawals_by_parent(db, user_id, [r.id for r in rows])

    lots: dict[str, list] = {}
    for r in rows:
        lots.setdefault(r.crypto, []).append(_lot(r, withdrawals.get(r.id)))
    holdings = []
    for crypto, lot_list in sorted(lots.items()):
        amount = sum(l["amount"] for l in lot_list)
        cost = sum(l["cost_usdt"] for l in lot_list)
        holdings.append({
            "crypto": crypto, "lots": lot_list,
            "total_amount": round(amount, 8),
            "total_cost_usdt": round(cost, 2),
            "average_price_usdt": round(cost / amount, 12) if amount else 0.0,   # 12: sub-cent coins keep 4 significant digits
        })
    return {
        "year": year, "method": "FIFO",
        "realized_gains_usdt": 0.0,  # buy-only platform: no disposals yet
        "note": ("No disposals occurred through AutoBuy; all positions are "
                 "unrealized. Lots are FIFO-ordered for future sales. "
                 "Estimates only, not financial or tax advice."),
        "acquisitions": holdings,
        "total_acquired_usdt": round(sum(h["total_cost_usdt"] for h in holdings), 2),
    }


async def performance(db, user_id, market) -> dict:
    """ROI and risk metrics from successful buys + live prices."""
    rows = await history.completed_rows(db, user_id)
    if not rows:
        return {"has_data": False}

    invested = sum(r.cost_usd for r in rows)
    amounts: dict[str, float] = {}
    for r in rows:
        amounts[r.crypto] = amounts.get(r.crypto, 0.0) + r.amount_crypto

    value = 0.0
    priced = True
    for crypto, amount in amounts.items():
        price = await market.get_price(crypto)
        if price is None:
            priced = False
            break
        value += amount * price

    daily: dict[str, float] = {}
    for r in rows:
        key = r.happened_at.date().isoformat()
        daily[key] = daily.get(key, 0.0) + r.cost_usd
    best_day = max(daily, key=lambda d: daily[d])
    first = rows[0].happened_at
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
        "note": "Estimates only, not financial advice; past performance doesn't guarantee future results.",
    }
    if priced and invested:
        roi = (value - invested) / invested
        out.update({
            "current_value_usdt": round(value, 2),
            "roi_pct": round(roi * 100, 2),
            "annualized_roi_pct": round(((1 + roi) ** (1 / years_held) - 1) * 100, 2),
        })
    return out


def _fmt(value, digits: int) -> str:
    """CSV cells are machine-readable: money keeps its 2 decimals; crypto amounts (8)
    are trimmed of trailing zeros; never any thousands grouping."""
    if value is None:
        return ""
    return history.fmt_amount_plain(value, digits) if digits >= 8 else f"{value:.{digits}f}"


def tax_summary_csv(summary: dict) -> str:
    """One row per successful buy with everything an accountant asks for."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for holding in summary["acquisitions"]:
        for lot in holding["lots"]:
            writer.writerow([
                lot["datetime_utc"], holding["crypto"], _fmt(lot["amount"], 8), _fmt(lot.get("net_amount"), 8),
                history.fmt_price_plain(lot["price"]), f"{lot['cost_usdt']:.2f}",
                _fmt(lot["fee"], 8), lot["fee_currency"] or "",
                lot["exchange"] or "", lot["order_id"] or "",
                lot["purchase_type"], lot["tx_type"],
                lot["withdrawal_address"] or "", lot["network"] or "",
                _fmt(lot["withdrawal_fee"], 8), lot["withdrawal_fee_currency"] or "",
                lot["tx_hash"] or "", lot.get("withdrawal_status") or "", summary["method"],
            ])
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
    data = [["Crypto", "Amount", "Cost (USD)", "Avg price"]]
    for h in summary["acquisitions"]:
        data.append([h["crypto"], history.fmt_amount(h['total_amount']),
                     f"{h['total_cost_usdt']:,.2f}",
                     history.fmt_price(h['average_price_usdt'])])
    data.append(["TOTAL", "", f"{summary['total_acquired_usdt']:.2f}", ""])
    table = Table(data, colWidths=[3 * cm, 5 * cm, 4 * cm, 4 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0E6E62")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story += [table, Spacer(1, 0.5 * cm)]
    # per-lot detail: date, order, exchange, and the withdrawal when there was one
    detail = [["Date (UTC)", "Crypto", "Qty", "Price", "USD", "Exchange", "Order", "Sent to"]]
    for h in summary["acquisitions"]:
        for lot in h["lots"]:
            sent = f"{lot['withdrawal_address'][:10]}… ({lot['network']})" if lot["withdrawal_address"] else "held"
            detail.append([lot["datetime_utc"], h["crypto"], _fmt(lot['amount'], 8), _fmt(lot["price"], 2),
                           f"{lot['cost_usdt']:.2f}", lot["exchange"] or "", (lot["order_id"] or "")[:14], sent])
    if len(detail) > 1:
        lots_table = Table(detail, colWidths=[3.2 * cm, 1.4 * cm, 2.4 * cm, 2 * cm, 1.8 * cm, 1.8 * cm, 2.6 * cm, 3 * cm])
        lots_table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5E7EB")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        story += [lots_table, Spacer(1, 0.5 * cm)]
    story.append(Paragraph(summary["note"], styles["Italic"]))
    doc.build(story)
    return buf.getvalue()
