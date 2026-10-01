import { Link } from "react-router-dom";
import { dashboardAPI } from "@/api/endpoints";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import Analytics from "@/components/Dashboard/Analytics";
import PortfolioChart from "@/components/Dashboard/PortfolioChart";
import { useAsync } from "@/hooks/useAsync";
import { capitalize, fmtCrypto, fmtDate, fmtDay, fmtUsdt } from "@/utils/format";

function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="card">
      <p className="text-sm text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}

export default function Dashboard() {
  const { data, loading, error, reload } = useAsync(
    () => Promise.all([dashboardAPI.quickStats(), dashboardAPI.overview()])
      .then(([stats, overview]) => ({ stats, overview })),
  );

  if (loading) return <Loading />;
  if (error || !data) return <Alert kind="error">{error} <button className="ml-2 underline" onClick={() => void reload()}>Retry</button></Alert>;
  const { stats, overview } = data;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <Link to="/purchases" className="btn-primary">New purchase</Link>
      </div>
      {stats.kyc_status !== "verified" && (
        <Alert kind="warning">Identity not verified — scheduled purchases will not execute. <Link to="/kyc" className="font-medium underline">Verify now</Link></Alert>
      )}
      {(stats.low_balance ?? []).map((n) => (
        <div key={n.purchase_id} data-testid="low-balance-banner">
          <Alert kind="warning">Balance low for your next {n.crypto_symbol} buy on {fmtDate(n.next_execution)}. Move USDT into the spot wallet of your exchange sub-account before then. <Link to="/purchases" className="font-medium underline">See purchases</Link></Alert>
        </div>
      ))}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Total invested" value={fmtUsdt(stats.total_invested_usdt)} />
        <Stat label="Transactions" value={stats.transaction_count} />
        <Stat label="Active schedules" value={stats.active_purchases} />
        <Stat label="Verification" value={<StatusBadge status={stats.kyc_status} />} hint={`Monthly average ${fmtUsdt(overview.monthly_average_usdt)}`} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="card" aria-label="Holdings">
          <h2 className="font-semibold">Holdings</h2>
          {overview.holdings.length === 0 ? <p className="mt-3 text-sm text-slate-500">No completed purchases yet.</p> : (
            <table className="mt-3 w-full text-sm">
              <thead className="text-left text-slate-500"><tr><th className="py-1">Asset</th><th>Acquired</th><th>Where it is</th><th className="text-right">Cost</th></tr></thead>
              <tbody>{overview.holdings.map((h) => (
                <tr key={h.crypto} className="border-t border-slate-100" data-testid={`holding-${h.crypto}`}>
                  <td className="py-2 font-medium">{h.crypto}</td>
                  <td title="Net of the trading fee charged in the coin">{fmtCrypto(h.total_amount)}</td>
                  <td className="text-slate-600">
                    {(h.sent_to_wallet ?? 0) > 0
                      ? <>{fmtCrypto(h.on_exchange ?? h.total_amount)} on exchange · {fmtCrypto(h.sent_to_wallet)} in your wallet</>
                      : "all on the exchange"}
                  </td>
                  <td className="text-right">{fmtUsdt(h.total_cost_usdt)}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
          {overview.holdings.length > 0 && (
            <p className="mt-3 text-xs text-slate-500" data-testid="holdings-note">
              AutoBuy's holdings count only what it bought for you, so they won't reflect coins you move or add yourself.
            </p>
          )}
        </section>
        <PortfolioChart />
      </div>
      <section className="card" aria-label="Upcoming purchases">
        <h2 className="font-semibold">Upcoming purchases</h2>
        {overview.upcoming_purchases.length === 0 ? <p className="mt-3 text-sm text-slate-500">Nothing scheduled. <Link to="/purchases" className="text-brand-600 underline">Create a schedule</Link>.</p> : (
          <ul className="mt-3 divide-y divide-slate-100 text-sm">{overview.upcoming_purchases.map((p) => (
            <li key={p.id} className="flex items-center justify-between py-2">
              <span><span className="font-medium">{p.crypto}</span> · {fmtUsdt(p.amount_usdt)} · {capitalize(p.frequency)}</span>
              <span className="text-slate-500">{fmtDay(p.next_purchase_date)}</span>
            </li>
          ))}</ul>
        )}
      </section>
      <Analytics />
    </div>
  );
}
