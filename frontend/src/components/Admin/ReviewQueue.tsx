import { useState } from "react";
import { adminAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import type { AdminPurchaseRow, PurchaseReview } from "@/types";
import { fmtDate, fmtUsdt } from "@/utils/format";

const ACTIONS: { key: "record_fill" | "mark_not_sent" | "cancel"; label: string; help: string; danger?: boolean }[] = [
  { key: "record_fill", label: "Record fill", help: "The exchange shows the order FILLED and the fill passes the guard: record it as the buy, never re-buy." },
  { key: "mark_not_sent", label: "Mark not sent", help: "The exchange shows NOT FOUND or CANCELLED: the attempt failed, the purchase becomes due again." },
  { key: "cancel", label: "Cancel purchase", help: "Stop the purchase; the attempt stays on record.", danger: true },
];

/** Purchases parked as needs_review: inspect what the exchange says, then resolve. */
export default function ReviewQueue() {
  const { data, loading, error, reload } = useAsync(() => adminAPI.purchases("needs_review"));
  const [review, setReview] = useState<PurchaseReview | null>(null);
  const [checking, setChecking] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function check(p: AdminPurchaseRow) {
    setMsg(null); setChecking(p.id); setReview(null);
    try { setReview(await adminAPI.reviewPurchase(p.id)); }
    catch (e) { setMsg({ kind: "error", text: errorMessage(e, "Could not check the exchange") }); }
    finally { setChecking(null); }
  }

  async function resolve(action: "record_fill" | "mark_not_sent" | "cancel") {
    if (!review) return;
    if (note.trim().length < 5) { setMsg({ kind: "error", text: "Write what you checked on the exchange (at least 5 characters) before resolving." }); return; }
    setBusy(true); setMsg(null);
    try {
      const p = await adminAPI.resolvePurchase(review.purchase.id, action, note.trim());
      setMsg({ kind: "success", text: `Purchase ${p.id.slice(0, 8)} resolved: now ${p.status}.` });
      setReview(null); setNote(""); await reload();
    } catch (e) { setMsg({ kind: "error", text: errorMessage(e, "The resolution was rejected — nothing changed.") }); }
    finally { setBusy(false); }
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-600">A parked purchase never retries on its own. Check what the exchange says about the order, then record the fill, mark it not sent, or cancel.</p>
      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}
      {loading ? <Loading /> : error || !data ? <Alert kind="error">{error}</Alert> : data.items.length === 0 ? (
        <p className="text-sm text-slate-500" data-testid="review-empty">Nothing is waiting for review.</p>
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm" data-testid="review-queue">
            <thead className="bg-slate-50 text-left text-slate-500"><tr>
              <th className="px-4 py-2">Purchase</th><th className="px-4 py-2">User</th><th className="px-4 py-2">Parked since</th><th className="px-4 py-2">Why</th><th className="px-4 py-2 text-right">Actions</th>
            </tr></thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id} className="border-t border-slate-100" data-testid="review-row">
                  <td className="px-4 py-2 font-medium">{p.crypto_symbol} {fmtUsdt(p.amount_usd)} <span className="text-xs font-normal text-slate-400">{p.purchase_type} · {p.frequency}</span></td>
                  <td className="px-4 py-2">{p.email ?? p.user_id.slice(0, 8)}</td>
                  <td className="px-4 py-2 text-xs">{fmtDate(p.updated_at)}</td>
                  <td className="max-w-md px-4 py-2 text-xs text-red-700">{p.last_error}</td>
                  <td className="whitespace-nowrap px-4 py-2 text-right"><button className="btn-secondary !py-1" disabled={checking === p.id} onClick={() => void check(p)}>{checking === p.id ? "Checking…" : "Check exchange"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {review && (
        <section className="card space-y-3" aria-label="Review purchase" data-testid="review-panel">
          <h3 className="font-semibold">Exchange says</h3>
          <ul className="space-y-2 text-sm">
            {review.lookups.map((l) => (
              <li key={l.attempt_id} className="rounded-md border border-slate-200 p-2">
                <div>attempt <span className="font-mono text-xs">{l.client_oid}</span>: <StatusBadge status={l.state ?? "unknown"} />{l.error && <span className="ml-2 text-xs text-red-600">{l.error}</span>}</div>
                {l.fill && <div className="mt-1 font-mono text-xs text-slate-600">amount {String(l.fill.amount_crypto)} · cost {String(l.fill.cost_usdt)} · avg {String(l.fill.exchange_rate)} · status {String(l.fill.raw_status)}</div>}
                {l.verdict && <div className={`mt-1 text-xs ${l.verdict === "ok" ? "text-green-700" : "text-red-600"}`} data-testid="verdict">{l.verdict === "ok" ? "fill passes the guard" : `fill would NOT be recorded: ${l.verdict}`}</div>}
              </li>
            ))}
          </ul>
          <div>
            <label className="label" htmlFor="review-note">What did you check on the exchange?</label>
            <input id="review-note" className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. order 123 shows filled 0.00019 BTC in Bitget order history" />
          </div>
          <div className="flex flex-wrap gap-2">
            {ACTIONS.map((a) => (
              <button key={a.key} className={`${a.danger ? "btn-danger" : "btn-primary"} !py-1`} title={a.help} disabled={busy} onClick={() => void resolve(a.key)}>{a.label}</button>
            ))}
            <button className="btn-secondary !py-1" disabled={busy} onClick={() => setReview(null)}>Close</button>
          </div>
        </section>
      )}
    </div>
  );
}
