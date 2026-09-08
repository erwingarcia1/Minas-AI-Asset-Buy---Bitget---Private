import type { AutoBuyPurchase } from "../../types";

const badge: Record<string, string> = {
  active: "bg-green-100 text-green-800", paused: "bg-gray-100 text-gray-700", completed: "bg-blue-100 text-blue-800",
  failed: "bg-red-100 text-red-800", cancelled: "bg-gray-100 text-gray-500",
};
const freq: Record<string, string> = { one_time: "One-time", daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
const fmtDate = (iso?: string | null) => iso ? new Date(iso).toLocaleString() : "—";

interface Props {
  purchases: AutoBuyPurchase[];
  onView: (p: AutoBuyPurchase) => void;
  onPause: (p: AutoBuyPurchase) => void;
  onResume: (p: AutoBuyPurchase) => void;
  onDelete: (p: AutoBuyPurchase) => void;
}

export default function PurchasesList({ purchases, onView, onPause, onResume, onDelete }: Props) {
  if (purchases.length === 0) return <p className="rounded-lg bg-white p-6 text-sm text-gray-500 shadow-sm">No auto-buys yet.</p>;
  return (
    <div className="overflow-x-auto rounded-lg bg-white shadow-sm">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left text-gray-500">
          <tr><th className="px-4 py-2">Crypto</th><th className="px-4 py-2">Amount</th><th className="px-4 py-2">Frequency</th>
              <th className="px-4 py-2">Network</th><th className="px-4 py-2">Status</th><th className="px-4 py-2">Next execution</th><th className="px-4 py-2 text-right">Actions</th></tr>
        </thead>
        <tbody>
          {purchases.map((p) => (
            <tr key={p.id} className="border-t" data-testid="purchase-row">
              <td className="px-4 py-2 font-medium">{p.crypto_symbol}<div className="font-mono text-xs text-gray-400">{p.wallet_address.slice(0, 10)}…</div></td>
              <td className="px-4 py-2">${p.amount_usd.toFixed(2)}</td>
              <td className="px-4 py-2">{freq[p.frequency] ?? p.frequency}</td>
              <td className="px-4 py-2">{p.blockchain_network}{p.exchange ? <span className="text-gray-400"> · {p.exchange}</span> : null}</td>
              <td className="px-4 py-2">
                <span className={`rounded-full px-2 py-0.5 text-xs ${badge[p.status] ?? ""}`}>{p.status}</span>
                {p.last_error && <div className="mt-1 max-w-xs truncate text-xs text-red-600" title={p.last_error}>{p.last_error}</div>}
              </td>
              <td className="px-4 py-2">{fmtDate(p.next_execution)}</td>
              <td className="space-x-2 whitespace-nowrap px-4 py-2 text-right">
                <button className="text-blue-600 hover:underline" onClick={() => onView(p)}>View transactions</button>
                {p.status === "active" && <button className="text-gray-700 hover:underline" onClick={() => onPause(p)}>Pause</button>}
                {p.status === "paused" && <button className="text-gray-700 hover:underline" onClick={() => onResume(p)}>Resume</button>}
                {!["cancelled", "completed"].includes(p.status) && <button className="text-red-600 hover:underline" onClick={() => onDelete(p)}>Delete</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
