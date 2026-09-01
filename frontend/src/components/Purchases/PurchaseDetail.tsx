import { useEffect, useState } from "react";
import { purchases, transactions } from "../../services/api";
import type { RecurringPurchase, Transaction } from "../../types";
import { StatusBadge } from "../Dashboard/Dashboard";
import Loading from "../common/Loading";

export default function PurchaseDetail({ id }: { id: string }) {
  const [purchase, setPurchase] = useState<RecurringPurchase | null>(null);
  const [history, setHistory] = useState<Transaction[]>([]);

  useEffect(() => {
    purchases.get(id).then(setPurchase);
    transactions.list(100, 0).then((txs) =>
      setHistory(txs.items.filter((t) => t.recurring_purchase_id === id)));
  }, [id]);

  if (!purchase) return <Loading />;
  return (
    <div className="space-y-4">
      <h2 className="text-lg font-medium">
        {purchase.crypto} — {purchase.amount_usdt} USDT {purchase.frequency}
      </h2>
      <dl className="grid grid-cols-2 gap-2 text-sm">
        <dt className="text-gray-500">Next buy</dt>
        <dd>{new Date(purchase.next_purchase_date).toLocaleString()}</dd>
        <dt className="text-gray-500">Withdrawal address</dt>
        <dd>{purchase.withdrawal_address ?? "—"}</dd>
        <dt className="text-gray-500">Status</dt>
        <dd>{purchase.enabled ? "enabled" : "paused"}</dd>
      </dl>
      <h3 className="font-medium">Executions</h3>
      <ul className="space-y-1 text-sm">
        {history.map((t) => (
          <li key={t.id} className="flex gap-2">
            <span>{new Date(t.created_at).toLocaleDateString()}</span>
            <StatusBadge status={t.status} />
            <span>{t.amount_crypto.toFixed(8)} for {t.cost_usdt} USDT</span>
          </li>
        ))}
        {history.length === 0 && <li className="text-gray-500">No executions yet.</li>}
      </ul>
    </div>
  );
}
