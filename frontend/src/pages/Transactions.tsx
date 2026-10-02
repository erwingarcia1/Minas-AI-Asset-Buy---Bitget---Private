import { useState } from "react";
import { transactionAPI } from "@/api/endpoints";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { fmtCrypto, fmtDate, fmtUsdt, fmtPrice } from "@/utils/format";

const PAGE = 20;

export default function Transactions() {
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<string>("");
  const { data, loading, error } = useAsync(
    () => transactionAPI.list({ limit: PAGE, offset, status_filter: status || undefined }),
    [offset, status],
  );
  const { data: stats } = useAsync(() => transactionAPI.stats());

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Transactions</h1>
        <select className="input w-auto" value={status} onChange={(e) => { setOffset(0); setStatus(e.target.value); }}>
          <option value="">All statuses</option><option value="completed">Completed</option><option value="pending">Pending</option><option value="failed">Failed</option><option value="simulated">Simulated (dry run)</option>
        </select>
      </div>
      {stats && (
        <div className="grid gap-4 sm:grid-cols-3">
          <div className="card" data-testid="tile-completed"><p className="text-sm text-slate-500">Completed buys</p><p className="text-2xl font-semibold">{stats.completed_count}</p>
            <p className="text-xs text-slate-400">{stats.failed_count} failed attempt{stats.failed_count === 1 ? "" : "s"}{(stats.needs_review_count ?? 0) > 0 ? ` · ${stats.needs_review_count} under review` : ""} · nothing spent on those</p></div>
          <div className="card" data-testid="tile-invested"><p className="text-sm text-slate-500">Total invested</p><p className="text-2xl font-semibold">{fmtUsdt(stats.total_invested_usdt)}</p><p className="text-xs text-slate-400">completed buys only</p></div>
          <div className="card" data-testid="tile-average"><p className="text-sm text-slate-500">Average per completed buy</p><p className="text-2xl font-semibold">{fmtUsdt(stats.average_purchase_usdt)}</p></div>
        </div>
      )}
      {loading ? <Loading /> : error || !data ? <Alert kind="error">{error}</Alert> : (
        <>
          <div className="card overflow-x-auto p-0">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-slate-500"><tr>
                <th className="px-4 py-2">Executed</th><th className="px-4 py-2">Asset</th><th className="px-4 py-2">Amount</th><th className="px-4 py-2">Cost</th><th className="px-4 py-2">Rate</th><th className="px-4 py-2">Delivery</th><th className="px-4 py-2">Status</th>
              </tr></thead>
              <tbody>
                {data.items.length === 0 && <tr><td colSpan={7} className="px-4 py-6 text-center text-slate-500">No transactions.</td></tr>}
                {data.items.map((t) => (
                  <tr key={t.id} className={`border-t border-slate-100 ${t.status === "failed" ? "bg-red-50/60 text-slate-500" : ""}`} data-testid={t.status === "failed" ? "failed-row" : undefined}>
                    <td className="px-4 py-2">{fmtDate(t.executed_at ?? t.created_at)}</td>
                    <td className="px-4 py-2 font-medium">{t.crypto}</td>
                    <td className="px-4 py-2">{t.status === "failed" || t.status === "pending" ? "—" : <>{fmtCrypto(t.amount_crypto)}{t.net_amount != null && t.net_amount !== t.amount_crypto && <div className="text-xs text-slate-400" title="After the trading fee charged in the coin">{fmtCrypto(t.net_amount)} received</div>}</>}</td>
                    <td className="px-4 py-2">{t.status === "failed" ? <span className="text-slate-400" title="A failed attempt spends nothing">— <span className="text-xs">nothing spent</span></span> : t.status === "pending" ? "—" : fmtUsdt(t.cost_usdt)}</td>
                    <td className="px-4 py-2">{t.exchange_rate && t.status !== "failed" ? fmtPrice(t.exchange_rate) : "—"}</td>
                    <td className="px-4 py-2 text-xs">
                      {t.status === "failed" ? <span className="font-medium text-red-700">Attempt failed — not bought</span> : t.tx_type === "withdrawal" ? <>{t.status === "simulated" ? "Simulated send to" : "Sent to"} <span className="font-mono" title={t.wallet_address ?? ""}>{(t.wallet_address ?? "").slice(0, 10)}…</span> ({t.blockchain_network})</> : t.tx_type === "buy" ? "Bought on exchange" : "Held on exchange"}
                      <div className="text-slate-400">{t.exchange ?? "—"}{t.order_id ? ` · order ${t.order_id}` : ""}{t.tx_hash ? ` · tx ${t.tx_hash.slice(0, 10)}…` : ""}</div>
                    </td>
                    <td className="px-4 py-2"><StatusBadge status={t.status} />{t.error_message && <p className="mt-1 text-xs text-red-600">{t.error_message}</p>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between text-sm text-slate-600">
            <span>{data.total === 0 ? "0 results" : `${offset + 1}–${Math.min(offset + PAGE, data.total)} of ${data.total}`}</span>
            <div className="space-x-2">
              <button className="btn-secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
              <button className="btn-secondary" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
