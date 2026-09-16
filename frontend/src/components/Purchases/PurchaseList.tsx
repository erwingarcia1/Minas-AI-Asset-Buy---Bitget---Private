import { tagLabelForChain } from "@/utils/chains";
import { useEffect, useState } from "react";
import { autoBuyAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { purchaseMode, type AutoBuyPurchase, type AutoBuyTransaction } from "@/types";
import { SwitchToWithdrawDialog } from "./SwitchModeDialog";
import { capitalize, fmtAmount, fmtDate } from "@/utils/format";

const FREQ: Record<string, string> = { one_time: "One-time", daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
/** Purchases parked after a filled buy whose withdrawal did not go through: the coins sit on the exchange. */
const PARKED: Record<string, string> = {
  withdrawal_failed: "The buy filled but the withdrawal failed, so the coins are still on the exchange. Fix the cause (key permissions, IP whitelist, address book, chain) and retry the withdrawal; the buy is never repeated.",
  needs_info: "The exchange wants beneficiary (travel-rule) details before it will send this withdrawal. Add them to the purchase, then retry; nothing is bought again.",
  needs_review: "The buy's outcome could not be confirmed on the exchange. Support is reviewing it; nothing is retried automatically.",
};
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
                <td className="whitespace-nowrap px-4 py-2 font-medium">{p.crypto_symbol}
                  <div className="text-xs font-normal text-slate-400" data-testid="purchase-venue">
                    {p.exchange_key_removed || (!p.exchange && !p.exchange_account_id) ? <span className="text-red-600">Exchange key removed</span> : capitalize(p.exchange ?? "")}
                    {purchaseMode(p) === "dca" ? ` · Held on ${capitalize(p.exchange ?? "exchange")}` : p.blockchain_network ? ` · ${p.blockchain_network}` : ""}
                    {purchaseMode(p) !== "dca" && p.wallet_destination_type ? (p.wallet_memo ? ` · ${tagLabelForChain(p.blockchain_network) ?? "memo"} ${p.wallet_memo}` : " · self-custody") : ""}
                  </div>
                </td>
                <td className="px-4 py-2">{fmtUsd(p.amount_usd)}</td>
                <td className="px-4 py-2">{FREQ[p.frequency] ?? capitalize(p.frequency)}</td>
                <td className="px-4 py-2">
                  <StatusBadge status={purchaseMode(p) === "autobuy" ? "autobuy" : purchaseMode(p) === "dca" ? "accumulate" : "unknown mode"} />
                  <div className="mt-1 font-mono text-xs text-slate-400" title={p.wallet_address ?? ""}>{p.wallet_address ? `${p.wallet_address.slice(0, 12)}…` : "no wallet on file"}</div>
                </td>
                <td className="px-4 py-2"><StatusBadge status={p.status} />{p.last_error && <div className="mt-1 max-w-xs truncate text-xs text-red-600" title={p.last_error}>{p.last_error}</div>}{PARKED[p.status] && <div className="mt-1 max-w-xs text-xs text-slate-500" data-testid="parked-help">{PARKED[p.status]}</div>}</td>
                <td className="whitespace-nowrap px-4 py-2 text-xs">{fmtDate(p.next_execution)}</td>
                <td className="space-x-2 whitespace-nowrap px-4 py-2 text-right">
                  <button className="btn-secondary !py-1" onClick={() => setOpen(open?.id === p.id ? null : p)}>{open?.id === p.id ? "Hide" : "Transactions"}</button>
                  {p.status === "active" && <button className="btn-secondary !py-1" onClick={() => void act(() => autoBuyAPI.update(p.id, { status: "paused" }))}>Pause</button>}
                  {p.status === "paused" && !p.exchange_key_removed && <button className="btn-secondary !py-1" onClick={() => void act(() => autoBuyAPI.update(p.id, { status: "active" }))}>Resume</button>}
                  {p.status === "paused" && p.exchange_key_removed && <button className="btn-secondary !py-1" disabled title="Exchange key removed: add a key and create a new purchase">Resume</button>}
                  {["withdrawal_failed", "needs_info"].includes(p.status) && !p.exchange_key_removed && (
                    <button className="btn-primary !py-1" onClick={() => { if (confirm(`Send the ${p.crypto_symbol} already bought to ${p.wallet_address ?? "the wallet on file"} on ${p.blockchain_network} again? No new buy is placed.`)) void act(() => autoBuyAPI.retryWithdrawal(p.id)); }}>Retry withdrawal</button>
                  )}
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
          <option value="">All statuses</option><option value="pending">Pending</option><option value="success">Success</option><option value="failed">Failed</option><option value="simulated">Simulated</option>
        </select>
      </div>
      {error && <Alert kind="error">{error}</Alert>}
      {rows === null && !error ? <Loading /> : rows && rows.length === 0 ? <p className="text-sm text-slate-500">No transactions{status ? ` with status "${status}"` : ""} yet.</p> : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-500"><tr><th className="py-1">Date</th><th>Amount</th><th>Status</th><th>Exchange ID / tx hash</th><th className="text-right">Fee</th></tr></thead>
          <tbody>{rows?.map((t) => (
            <tr key={t.id} className="border-t border-slate-100">
              <td className="py-2">{fmtDate(t.executed_at ?? t.created_at)}</td>
              <td>{t.amount_executed != null ? `${fmtAmount(t.amount_executed)} ${purchase.crypto_symbol}` : "—"}{t.tx_type === "withdrawal" && t.expected_received != null && t.expected_received !== t.amount_executed && <div className="text-xs text-slate-400">≈ {fmtAmount(t.expected_received)} {purchase.crypto_symbol} received</div>}{t.tx_type === "withdrawal" && t.withdrawal_memo && <div className="text-xs text-slate-400">{tagLabelForChain(t.withdrawal_chain) ?? "memo"} {t.withdrawal_memo}</div>}</td>
              <td><StatusBadge status={t.status} /> {t.tx_type && <span className="text-xs text-slate-400">{t.tx_type}</span>}{t.error_message && <div className="text-xs text-red-600">{t.error_message}</div>}</td>
              <td className="font-mono text-xs">{t.exchange_tx_id ?? "—"}{t.tx_type === "withdrawal" && <div className="text-slate-400" title="on-chain transaction hash">{t.tx_hash ? `tx ${t.tx_hash.slice(0, 18)}…` : t.status === "simulated" ? "dry run: nothing was sent" : `awaiting tx hash${t.raw_status ? ` · ${t.raw_status}` : ""}`}</div>}</td>
              <td className="text-right">{t.tx_type === "withdrawal" ? (t.withdrawal_fee != null ? `${fmtAmount(t.withdrawal_fee)} ${t.withdrawal_fee_currency ?? ""}` : "—") : t.fee != null ? `${fmtAmount(t.fee)} ${t.fee_currency ?? ""}` : "—"}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </section>
  );
}

