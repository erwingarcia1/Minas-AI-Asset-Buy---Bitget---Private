import { useEffect, useState } from "react";
import { apiErrorMessage, autoBuy } from "../../services/api";
import type { AutoBuyPurchase, AutoBuyTransaction } from "../../types";

const badge: Record<string, string> = { pending: "bg-amber-100 text-amber-800", success: "bg-green-100 text-green-800", failed: "bg-red-100 text-red-800" };
const fmtDate = (iso?: string | null) => iso ? new Date(iso).toLocaleString() : "—";

export default function TransactionHistory({ purchase, onClose }: { purchase: AutoBuyPurchase; onClose: () => void }) {
  const [status, setStatus] = useState("");
  const [rows, setRows] = useState<AutoBuyTransaction[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setRows(null);
    autoBuy.transactions(purchase.id, status).then(setRows).catch((e) => setError(apiErrorMessage(e)));
  }, [purchase.id, status]);

  return (
    <section className="rounded-lg bg-white p-6 shadow-sm" aria-label="Transaction history">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-medium">Transactions · {purchase.crypto_symbol} ${purchase.amount_usd.toFixed(2)}</h2>
        <div className="flex items-center gap-2">
          <select className="rounded border px-2 py-1 text-sm" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
            <option value="">All statuses</option><option value="pending">Pending</option><option value="success">Success</option><option value="failed">Failed</option>
          </select>
          <button className="text-sm text-gray-600 hover:underline" onClick={onClose}>Close</button>
        </div>
      </div>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {rows === null && !error ? <p className="text-sm text-gray-500">Loading…</p> : rows && rows.length === 0 ? <p className="text-sm text-gray-500">No transactions{status ? ` with status "${status}"` : ""}.</p> : (
        <table className="w-full text-sm">
          <thead className="text-left text-gray-500"><tr><th className="py-1">Date</th><th>Amount</th><th>Status</th><th>Exchange TX ID</th><th className="text-right">Fee</th></tr></thead>
          <tbody>
            {rows?.map((t) => (
              <tr key={t.id} className="border-t">
                <td className="py-2">{fmtDate(t.executed_at ?? t.created_at)}</td>
                <td>{t.amount_executed != null ? `${t.amount_executed.toLocaleString(undefined, { maximumFractionDigits: 8 })} ${purchase.crypto_symbol}` : "—"}</td>
                <td><span className={`rounded-full px-2 py-0.5 text-xs ${badge[t.status] ?? ""}`}>{t.status}</span>{t.error_message && <div className="text-xs text-red-600">{t.error_message}</div>}</td>
                <td className="font-mono text-xs">{t.exchange_tx_id ?? "—"}</td>
                <td className="text-right">{t.fee != null ? t.fee : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
