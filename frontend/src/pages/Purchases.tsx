import { useCallback, useEffect, useState } from "react";
import CreatePurchase from "../components/AutoBuy/CreatePurchase";
import PurchasesList from "../components/AutoBuy/PurchasesList";
import TransactionHistory from "../components/AutoBuy/TransactionHistory";
import { apiErrorMessage, autoBuy } from "../services/api";
import type { AutoBuyPurchase } from "../types";

/** Auto-Buy: buy on an exchange account and deliver to a validated wallet on a chosen network. */
export default function Purchases() {
  const [purchases, setPurchases] = useState<AutoBuyPurchase[]>([]);
  const [selected, setSelected] = useState<AutoBuyPurchase | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => autoBuy.list().then(setPurchases).catch((e) => setError(apiErrorMessage(e))), []);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<unknown>, done: string) {
    setError(null);
    try { await fn(); setNotice(done); await load(); } catch (e) { setError(apiErrorMessage(e)); }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Auto-Buy</h1>
      {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
      {notice && <p role="status" className="rounded bg-green-50 p-2 text-sm text-green-700">{notice}</p>}
      <CreatePurchase onCreated={(r) => { setNotice(`Auto-buy created (${r.status}); first run ${r.next_execution ? new Date(r.next_execution).toLocaleString() : "soon"}.`); void load(); }} />
      <PurchasesList
        purchases={purchases}
        onView={(p) => setSelected(p)}
        onPause={(p) => void act(() => autoBuy.update(p.id, { status: "paused" }), "Paused.")}
        onResume={(p) => void act(() => autoBuy.update(p.id, { status: "active" }), "Resumed.")}
        onDelete={(p) => { if (window.confirm(`Cancel the ${p.crypto_symbol} auto-buy? Its history is kept.`)) void act(() => autoBuy.remove(p.id), "Cancelled."); }}
      />
      {selected && <TransactionHistory purchase={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
