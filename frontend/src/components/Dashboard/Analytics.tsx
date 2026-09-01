import { useCallback, useEffect, useState } from "react";
import { analytics } from "../../services/api";
import type { Performance, RecurringProjection, VolatilityEntry } from "../../services/api";
import ErrorRetry from "../common/ErrorRetry";
import Loading from "../common/Loading";

const usd = (n: number) =>
  n.toLocaleString(undefined, { style: "currency", currency: "USD" });
const pnlTone = (n?: number | null) =>
  n == null ? "" : n >= 0 ? "text-teal-700" : "text-red-700";

export default function Analytics() {
  const [perf, setPerf] = useState<Performance | null>(null);
  const [recurring, setRecurring] = useState<RecurringProjection | null>(null);
  const [vol, setVol] = useState<VolatilityEntry[] | null>(null);
  const [diversification, setDiversification] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    Promise.all([analytics.performance(), analytics.recurring(),
                 analytics.volatility(), analytics.comparison()])
      .then(([p, r, v, c]) => {
        setPerf(p); setRecurring(r); setVol(v.entries);
        setDiversification(c.diversification_score);
      })
      .catch(() => setError("Couldn't load analytics."));
  }, []);
  useEffect(load, [load]);

  if (error) return <ErrorRetry message={error} onRetry={load} />;
  if (!perf || !recurring || !vol) return <Loading label="Crunching numbers…" />;

  return (
    <section aria-label="Analytics" className="space-y-6">
      <div className="rounded-lg bg-white p-4 shadow-sm">
        <h2 className="mb-3 font-medium">Performance</h2>
        {!perf.price_available && (
          <p className="mb-2 rounded bg-amber-50 p-2 text-xs text-amber-700">
            Live prices are unavailable right now — showing cost basis only.
          </p>
        )}
        <div className="grid gap-4 sm:grid-cols-3">
          <div>
            <div className="text-xs uppercase text-gray-500">Invested</div>
            <div className="text-xl font-semibold">{usd(perf.total_invested_usdt)}</div>
          </div>
          <div>
            <div className="text-xs uppercase text-gray-500">Market value</div>
            <div className="text-xl font-semibold">
              {perf.total_market_value != null ? usd(perf.total_market_value) : "—"}
            </div>
          </div>
          <div>
            <div className="text-xs uppercase text-gray-500">Unrealized P&L</div>
            <div className={`text-xl font-semibold ${pnlTone(perf.total_pnl_usdt)}`}>
              {perf.total_pnl_usdt != null
                ? `${usd(perf.total_pnl_usdt)} (${perf.total_pnl_pct}%)` : "—"}
            </div>
          </div>
        </div>
        {perf.best_performer && (
          <p className="mt-2 text-sm text-gray-600">
            Best: <b>{perf.best_performer}</b> · Worst: <b>{perf.worst_performer}</b>
            {diversification != null && <> · Diversification score: <b>{diversification}</b>/100</>}
          </p>
        )}
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-gray-500">
              <tr><th className="py-1">Crypto</th><th>Avg buy</th><th>Now</th><th>P&L</th></tr>
            </thead>
            <tbody>
              {perf.holdings.map((h) => (
                <tr key={h.crypto} className="border-t">
                  <td className="py-2 font-medium">{h.crypto}</td>
                  <td>{usd(h.average_buy_price)}</td>
                  <td>{h.current_price != null ? usd(h.current_price) : "—"}</td>
                  <td className={pnlTone(h.unrealized_pnl_pct)}>
                    {h.unrealized_pnl_pct != null ? `${h.unrealized_pnl_pct}%` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="rounded-lg bg-white p-4 shadow-sm">
          <h2 className="mb-3 font-medium">Spend projection</h2>
          <p className="text-sm">
            Expected monthly: <b>{usd(recurring.expected_monthly_usdt)}</b><br />
            Projected annual: <b>{usd(recurring.projected_annual_usdt)}</b>
          </p>
        </div>
        <div className="rounded-lg bg-white p-4 shadow-sm">
          <h2 className="mb-3 font-medium">Volatility guidance</h2>
          <ul className="space-y-2 text-sm">
            {vol.map((v) => (
              <li key={v.crypto}>
                <b>{v.crypto}</b>{" "}
                <span className={
                  v.level === "high" ? "text-red-700"
                  : v.level === "moderate" ? "text-amber-600" : "text-gray-500"}>
                  ({v.level}{v.volatility_30d_pct != null ? ` · ${v.volatility_30d_pct}%` : ""})
                </span>
                <span className="block text-gray-600">{v.recommendation}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
