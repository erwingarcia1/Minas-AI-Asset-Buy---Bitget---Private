import { useCallback, useEffect, useState } from "react";
import { purchases } from "../../services/api";
import type { RecurringPurchase } from "../../types";
import ErrorRetry from "../common/ErrorRetry";
import Loading from "../common/Loading";
import PurchaseForm from "./PurchaseForm";

export default function PurchaseList() {
  const [items, setItems] = useState<RecurringPurchase[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<RecurringPurchase | "new" | null>(null);

  const load = useCallback(() => {
    setError(null);
    purchases.list().then(setItems).catch(() => setError("Couldn't load your schedules."));
  }, []);
  useEffect(load, [load]);

  async function remove(p: RecurringPurchase) {
    if (!window.confirm(`Delete the ${p.crypto} schedule? Its transaction history is kept.`)) return;
    await purchases.remove(p.id);
    load();
  }

  async function toggle(p: RecurringPurchase) {
    const updated = await purchases.toggle(p.id);
    setItems((prev) => prev?.map((x) => (x.id === updated.id ? updated : x)) ?? null);
  }

  if (error) return <ErrorRetry message={error} onRetry={load} />;
  if (!items) return <Loading label="Loading schedules…" />;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Recurring purchases</h1>
        <button onClick={() => setEditing("new")}
                className="rounded bg-blue-600 px-4 py-2 text-sm text-white">
          Add schedule
        </button>
      </div>

      {editing && (
        <PurchaseForm
          existing={editing === "new" ? undefined : editing}
          onSaved={() => { setEditing(null); load(); }}
          onCancel={() => setEditing(null)}
        />
      )}

      <div className="overflow-x-auto rounded-lg bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-gray-500">
            <tr>
              <th className="p-3">Crypto</th><th>Amount</th><th>Frequency</th>
              <th>Next buy</th><th>Status</th><th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.map((p) => (
              <tr key={p.id} className="border-t">
                <td className="p-3 font-medium">{p.crypto}</td>
                <td>{p.amount_usdt.toLocaleString()} USDT</td>
                <td>{p.frequency}</td>
                <td>{new Date(p.next_purchase_date).toLocaleDateString()}</td>
                <td>
                  <span className={`rounded-full px-2 py-0.5 text-xs ${
                    p.enabled ? "bg-teal-50 text-teal-700" : "bg-gray-100 text-gray-500"}`}>
                    {p.enabled ? "enabled" : "paused"}
                  </span>
                </td>
                <td className="p-3 text-right">
                  <div className="flex justify-end gap-2 text-xs">
                    <button onClick={() => setEditing(p)} className="text-blue-600">Edit</button>
                    <button onClick={() => toggle(p)} className="text-amber-600">
                      {p.enabled ? "Pause" : "Resume"}
                    </button>
                    <button onClick={() => remove(p)} className="text-red-600">Delete</button>
                  </div>
                </td>
              </tr>
            ))}
            {items.length === 0 && !editing && (
              <tr><td colSpan={6} className="p-4 text-gray-500">
                No schedules yet — add your first one.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
