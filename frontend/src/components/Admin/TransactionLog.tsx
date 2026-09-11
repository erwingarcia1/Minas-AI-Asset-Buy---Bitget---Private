import { useState } from "react";
import { adminAPI } from "@/api/endpoints";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { fmtDate, fmtUsdt } from "@/utils/format";

const PAGE = 100;

export default function TransactionLog() {
  const [status, setStatus] = useState("");
  const { data, loading, error } = useAsync(() => adminAPI.transactions({ status_filter: status || undefined, limit: PAGE }), [status]);

  function exportCsv() {
    if (!data) return;
    const header = "id,user_id,crypto,cost_usdt,status,error,created_at";
    const lines = data.items.map((t) => [t.id, t.user_id, t.crypto, t.cost_usdt, t.status, JSON.stringify(t.error_message ?? ""), t.created_at].join(","));
    const blob = new Blob([[header, ...lines].join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "transactions.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <select className="input w-auto" aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option><option value="completed">completed</option><option value="failed">failed</option><option value="pending">pending</option>
        </select>
        <button className="btn-secondary" onClick={exportCsv} disabled={!data || data.items.length === 0}>Export CSV</button>
        {data && <span className="text-xs text-slate-500">{data.total} row(s){data.total > PAGE ? `, showing the latest ${PAGE}` : ""}</span>}
      </div>
      {loading ? <Loading /> : error || !data ? <Alert kind="error">{error}</Alert> : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm" data-testid="admin-transactions">
            <thead className="bg-slate-50 text-left text-slate-500"><tr>
              <th className="px-4 py-2">When</th><th className="px-4 py-2">User</th><th className="px-4 py-2">Asset</th><th className="px-4 py-2">Cost</th><th className="px-4 py-2">Status</th><th className="px-4 py-2">Error</th>
            </tr></thead>
            <tbody>
              {data.items.length === 0 && <tr><td colSpan={6} className="px-4 py-6 text-center text-slate-500">No transactions.</td></tr>}
              {data.items.map((t) => (
                <tr key={t.id} className="border-t border-slate-100">
                  <td className="whitespace-nowrap px-4 py-2">{fmtDate(t.created_at)}</td>
                  <td className="px-4 py-2 font-mono text-xs" title={t.user_id}>{t.user_id.slice(0, 8)}…</td>
                  <td className="px-4 py-2 font-medium">{t.crypto}</td>
                  <td className="px-4 py-2">{fmtUsdt(t.cost_usdt)}</td>
                  <td className="px-4 py-2"><StatusBadge status={t.status} /></td>
                  <td className="max-w-xs truncate px-4 py-2 text-xs text-red-600" title={t.error_message ?? ""}>{t.error_message ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
