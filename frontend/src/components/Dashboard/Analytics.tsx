import { analyticsAPI } from "@/api/endpoints";
import { Alert, Loading } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { fmtPrice, fmtUsdt } from "@/utils/format";

const pnlTone = (n?: number | null) => (n == null ? "" : n >= 0 ? "text-green-700" : "text-red-700");

/** Shown under every projection or performance figure: these are estimates, not advice. */
export function EstimatesNote() {
  return (
    <p className="text-xs text-slate-500" data-testid="estimates-note">
      Estimates only, not financial advice; past performance doesn't guarantee future results.
    </p>
  );
}

export default function Analytics() {
  const { data, loading, error, reload } = useAsync(() =>
    Promise.all([analyticsAPI.performance(), analyticsAPI.recurring(), analyticsAPI.volatility(), analyticsAPI.comparison()])
      .then(([perf, recurring, vol, comparison]) => ({ perf, recurring, vol: vol.entries, shares: comparison.share_of_invested })),
  );

  if (loading) return <Loading label="Crunching numbers…" />;
  if (error || !data) {
    return <Alert kind="error">Couldn't load analytics. <button className="ml-2 underline" onClick={() => void reload()}>Retry</button></Alert>;
  }
  const { perf, recurring, vol, shares } = data;

  return (
    <section aria-label="Analytics" className="space-y-6">
      <div className="card">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="font-semibold">Performance</h2>
          <EstimatesNote />
        </div>
        {!perf.price_available && (
          <Alert kind="warning">Live prices are unavailable right now — showing cost basis only.</Alert>
        )}
        <div className="mt-3 grid gap-4 sm:grid-cols-3">
          <div>
            <p className="text-sm text-slate-500">Invested</p>
            <p className="text-xl font-semibold">{fmtUsdt(perf.total_invested_usdt)}</p>
          </div>
          <div>
            <p className="text-sm text-slate-500">Market value</p>
            <p className="text-xl font-semibold">{perf.total_market_value != null ? fmtUsdt(perf.total_market_value) : "—"}</p>
          </div>
          <div>
            <p className="text-sm text-slate-500">Unrealized P&amp;L</p>
            <p className={`text-xl font-semibold ${pnlTone(perf.total_pnl_usdt)}`}>
              {perf.total_pnl_usdt != null ? `${fmtUsdt(perf.total_pnl_usdt)} (${perf.total_pnl_pct}%)` : "—"}
            </p>
          </div>
        </div>
        {shares.length > 0 && (
          <p className="mt-2 text-sm text-slate-600" data-testid="share-of-invested">
            Share of invested value (by cost, not current value): {shares.map((s, i) => <span key={s.crypto}>{i > 0 ? " · " : ""}<b>{s.crypto}</b> {s.share_pct}%</span>)}
          </p>
        )}
        {perf.holdings.length > 0 && (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-slate-500"><tr><th className="py-1">Asset</th><th>Avg buy</th><th>Now</th><th>P&amp;L</th></tr></thead>
              <tbody>
                {perf.holdings.map((h) => (
                  <tr key={h.crypto} className="border-t border-slate-100">
                    <td className="py-2 font-medium">{h.crypto}</td>
                    <td>{fmtPrice(h.average_buy_price, h.price_precision)}</td>
                    <td>{h.current_price != null ? fmtPrice(h.current_price, h.price_precision) : "—"}</td>
                    <td className={pnlTone(h.unrealized_pnl_pct)}>{h.unrealized_pnl_pct != null ? `${h.unrealized_pnl_pct}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="card space-y-2">
          <h2 className="font-semibold">Spend projection</h2>
          <p className="text-sm">
            Expected monthly: <b>{fmtUsdt(recurring.expected_monthly_usdt)}</b><br />
            Projected annual: <b>{fmtUsdt(recurring.projected_annual_usdt)}</b>
          </p>
          <EstimatesNote />
        </div>
        <div className="card space-y-2">
          <h2 className="font-semibold">30-day volatility</h2>
          {vol.length === 0 ? <p className="text-sm text-slate-500">No holdings or scheduled purchases yet.</p> : (
            <ul className="space-y-2 text-sm">
              {vol.map((v) => (
                <li key={v.crypto}>
                  <b>{v.crypto}</b>{" "}
                  <span className={v.level === "high" ? "text-red-700" : v.level === "moderate" ? "text-amber-600" : "text-slate-500"}>
                    ({v.level}{v.volatility_30d_pct != null ? ` · ${v.volatility_30d_pct}%` : ""})
                  </span>
                  <span className="block text-slate-600">{v.summary}</span>
                </li>
              ))}
            </ul>
          )}
          <EstimatesNote />
        </div>
      </div>
    </section>
  );
}
