import { useEffect, useState } from "react";
import { autoBuyAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { purchaseMode, type AutoBuyPurchase, type AutoBuyTransaction } from "@/types";
import { SwitchToWithdrawDialog } from "./SwitchModeDialog";
import { capitalize, fmtDate } from "@/utils/format";

const FREQ: Record<string, string> = { one_time: "One-time", daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
const fmtUsd = (n: number) => `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

interface Props {
  purchases: AutoBuyPurchase[];
  onChanged: () => void;
  onError: (msg: string) => void;
}

export default function PurchaseList({ purchases, onChanged, onError }: Props) {
  const [open, setOpen] = useState<AutoBuyPurchase | null>(null);
  const [switching, setSwitching] = useState<AutoBuyPurchase | null>(null);
  const apiLacksModes = purchases.length > 0 && purchases.every((p) => purchaseMode(p) === null);

  async function act(fn: () => Promise<unknown>) {
    try { await fn(); onChanged(); } catch (e) { onError(errorMessage(e)); }
  }

  return (
    <div className="space-y-4">
      {apiLacksModes && (
        <Alert kind="warning">
          The API this app is talking to predates purchase modes: it ignores the "Withdraw immediately" choice and
          reports no mode, so every purchase runs as autobuy. Rebuild the API container from the latest <code>main</code>
          (migrations 011 and 012) to enable accumulate mode.
        </Alert>
      )}
      <div className="card overflow-x-auto p-0">
        <table className="w-full text-sm" data-testid="purchase-table">
          <thead className="bg-slate-50 text-left text-slate-500"><tr>
            <th className="px-4 py-2">Crypto</th><th className="px-4 py-2">Amount</th><th className="px-4 py-2">Frequency</th>
            <th className="px-4 py-2">Mode</th><th className="px-4 py-2">Status</th><th className="whitespace-nowrap px-4 py-2">Next execution</th><th className="px-4 py-2 text-right">Actions</th>
          </tr></thead>
          <tbody>
            {purchases.length === 0 && <tr><td colSpan={7} className="px-4 py-6 text-center text-slate-500">No purchases yet.</td></tr>}
            {purchases.map((p) => (
              <tr key={p.id} className="border-t border-slate-100" data-testid="purchase-row">
                <td className="whitespace-nowrap px-4 py-2 font-medium">{p.crypto_symbol}<div className="text-xs font-normal text-slate-400">{p.exchange ?? "—"} · {p.blockchain_network}</div></td>
                <td className="px-4 py-2">{fmtUsd(p.amount_usd)}</td>
                <td className="px-4 py-2">{FREQ[p.frequency] ?? capitalize(p.frequency)}</td>
                <td className="px-4 py-2">
                  <StatusBadge status={purchaseMode(p) === "autobuy" ? "autobuy" : purchaseMode(p) === "dca" ? "accumulate" : "unknown mode"} />
                  <div className="mt-1 font-mono text-xs text-slate-400" title={p.wallet_address ?? ""}>{p.wallet_address ? `${p.wallet_address.slice(0, 12)}…` : "no wallet on file"}</div>
                </td>
                <td className="px-4 py-2"><StatusBadge status={p.status} />{p.last_error && <div className="mt-1 max-w-xs truncate text-xs text-red-600" title={p.last_error}>{p.last_error}</div>}</td>
                <td className="whitespace-nowrap px-4 py-2 text-xs">{fmtDate(p.next_execution)}</td>
                <td className="space-x-2 whitespace-nowrap px-4 py-2 text-right">
                  <button className="btn-secondary !py-1" onClick={() => setOpen(open?.id === p.id ? null : p)}>{open?.id === p.id ? "Hide" : "Transactions"}</button>
                  {p.status === "active" && <button className="btn-secondary !py-1" onClick={() => void act(() => autoBuyAPI.update(p.id, { status: "paused" }))}>Pause</button>}
                  {p.status === "paused" && <button className="btn-secondary !py-1" onClick={() => void act(() => autoBuyAPI.update(p.id, { status: "active" }))}>Resume</button>}
                  {["active", "paused"].includes(p.status) && purchaseMode(p) === "dca" && (
                    <button className="btn-secondary !py-1" onClick={() => setSwitching(p)}>Switch to withdraw</button>
                  )}
                  {["active", "paused"].includes(p.status) && purchaseMode(p) === "autobuy" && (
                    <button className="btn-secondary !py-1" onClick={() => { if (confirm(`Keep future ${p.crypto_symbol} buys on the exchange instead of withdrawing them?`)) void act(() => autoBuyAPI.update(p.id, { purchase_type: "dca" })); }}>Switch to accumulate</button>
                  )}
                  {!["cancelled", "completed"].includes(p.status) && (
                    <button className="btn-danger !py-1" onClick={() => { if (confirm(`Cancel the ${p.crypto_symbol} purchase? Its history is kept.`)) void act(() => autoBuyAPI.remove(p.id)); }}>Delete</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {open && <TransactionHistory purchase={open} />}
      {switching && <SwitchToWithdrawDialog purchase={switching} onDone={() => { setSwitching(null); onChanged(); }} onCancel={() => setSwitching(null)} />}
    </div>
  );
}

function TransactionHistory({ purchase }: { purchase: AutoBuyPurchase }) {
  const [status, setStatus] = useState("");
  const [rows, setRows] = useState<AutoBuyTransaction[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setRows(null); setError(null);
    autoBuyAPI.transactions(purchase.id, status || undefined).then(setRows).catch((e) => setError(errorMessage(e)));
  }, [purchase.id, status]);

  return (
    <section className="card" aria-label="Transaction history">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">Transactions · {purchase.crypto_symbol} {fmtUsd(purchase.amount_usd)} · {purchaseMode(purchase) === "autobuy" ? "autobuy: sent to wallet" : purchaseMode(purchase) === "dca" ? "accumulate on exchange" : "mode unknown (outdated API)"}</h3>
        <select className="input w-auto" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
          <option value="">All statuses</option><option value="pending">Pending</option><option value="success">Success</option><option value="failed">Failed</option>
        </select>
      </div>
      {error && <Alert kind="error">{error}</Alert>}
      {rows === null && !error ? <Loading /> : rows && rows.length === 0 ? <p className="text-sm text-slate-500">No transactions{status ? ` with status "${status}"` : ""} yet.</p> : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-500"><tr><th className="py-1">Date</th><th>Amount</th><th>Status</th><th>Exchange TX ID</th><th className="text-right">Fee</th></tr></thead>
          <tbody>{rows?.map((t) => (
            <tr key={t.id} className="border-t border-slate-100">
              <td className="py-2">{fmtDate(t.executed_at ?? t.created_at)}</td>
              <td>{t.amount_executed != null ? `${t.amount_executed.toLocaleString(undefined, { maximumFractionDigits: 8 })} ${purchase.crypto_symbol}` : "—"}</td>
              <td><StatusBadge status={t.status} /> {t.tx_type && <span className="text-xs text-slate-400">{t.tx_type}</span>}{t.error_message && <div className="text-xs text-red-600">{t.error_message}</div>}</td>
              <td className="font-mono text-xs">{t.exchange_tx_id ?? "—"}</td>
              <td className="text-right">{t.fee != null ? t.fee : "—"}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </section>
  );
}

