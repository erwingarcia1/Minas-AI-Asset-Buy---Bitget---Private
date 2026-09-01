import { useCallback, useEffect, useState } from "react";
import { admin } from "../../services/api";
import type { AdminTxRow } from "../../services/api";

export default function TransactionLog() {
  const [rows, setRows] = useState<AdminTxRow[]>([]);
  const [status, setStatus] = useState("");
  const [total, setTotal] = useState(0);

  const load = useCallback(() => {
    admin.transactions({ status_filter: status || undefined, limit: 100 })
      .then((r) => { setRows(r.items); setTotal(r.total); });
  }, [status]);
  useEffect(load, [load]);

  function exportCsv() {
    const header = "id,user_id,crypto,cost_usdt,status,error,created_at";
    const lines = rows.map((t) =>
      [t.id, t.user_id, t.crypto, t.cost_usdt, t.status,
       JSON.stringify(t.error_message ?? ""), t.created_at].join(","));
    const blob = new Blob([[header, ...lines].join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "transactions.csv";
    a.click();
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <select value={status} onChange={(e) => setStatus(e.target.value)}
                aria-label="Status filter" className="rounded border p-2 text-sm">
          <option value="">All statuses</option>
          <option>completed</option><option>failed</option><option>pending</option>
        </select>
        <button onClick={exportCsv} className="rounded border px-3 py-2 text-sm">
          Export CSV
        </button>
        <span className="text-xs text-gray-500">{total} row(s)</span>
      </div>
      <div className="overflow-x-auto rounded-lg bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-gray-500">
            <tr><th className="p-3">When</th><th>User</th><th>Crypto</th>
                <th>Cost</th><th>Status</th><th>Error</th></tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id} className="border-t">
                <td className="p-3">{new Date(t.created_at).toLocaleString()}</td>
                <td className="font-mono text-xs">{t.user_id.slice(0, 8)}…</td>
                <td>{t.crypto}</td>
                <td>${t.cost_usdt.toFixed(2)}</td>
                <td>{t.status}</td>
                <td className="text-red-700">{t.error_message ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
