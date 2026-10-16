import { useState } from "react";
import { adminAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import type { AdminUserRow } from "@/types";
import { fmtDate, fmtDay, fmtUsdt } from "@/utils/format";
import { ConfirmEmailDialog } from "./ConfirmEmailDialog";

type Pending = { kind: "suspend" | "delete" | "lift_hold" | "reset_2fa"; user: AdminUserRow } | null;
const HOLD_REASON_MIN = 5;

export default function UserManagement() {
  const [query, setQuery] = useState("");
  const { data, loading, error, reload } = useAsync(() => adminAPI.users(query), [query]);
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [holdReason, setHoldReason] = useState("");
  const [resetCode, setResetCode] = useState("");   // the admin's own authenticator code; the server needs it while their account has two-factor on

  async function unsuspend(u: AdminUserRow) {
    setNotice(null);
    try { await adminAPI.unsuspend(u.id); setNotice(`${u.email} unsuspended.`); await reload(); }
    catch (err) { setNotice(errorMessage(err)); }
  }

  async function confirmPending(typed: string) {
    if (!pending) return;
    if (pending.kind === "lift_hold" && holdReason.trim().length < HOLD_REASON_MIN) {
      setDialogError("Write how the customer proved their identity (at least 5 characters) before lifting the hold.");
      return;
    }
    setBusy(true); setDialogError(null);
    try {
      if (pending.kind === "delete") await adminAPI.removeUser(pending.user.id, typed);
      else if (pending.kind === "lift_hold") await adminAPI.liftWalletHold(pending.user.id, typed, holdReason.trim());
      else if (pending.kind === "reset_2fa") await adminAPI.resetTwoFactor(pending.user.id, resetCode.trim() || undefined);
      else await adminAPI.suspend(pending.user.id);
      setNotice(pending.kind === "delete" ? `${pending.user.email} deleted.`
        : pending.kind === "lift_hold" ? `Wallet-change hold lifted for ${pending.user.email}; they have been emailed.`
        : pending.kind === "reset_2fa" ? `Two-factor authentication reset for ${pending.user.email}; they have been emailed.`
        : `${pending.user.email} suspended.`);
      setHoldReason(""); setResetCode("");
      setPending(null);
      await reload();
    } catch (err) {
      setDialogError(errorMessage(err, "The action was rejected — nothing changed."));
    } finally { setBusy(false); }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <input className="input max-w-sm" placeholder="Search by email…" aria-label="Search users" value={query} onChange={(e) => setQuery(e.target.value)} />
        {data && <span className="text-xs text-slate-500">{data.total} user(s)</span>}
      </div>
      {notice && <Alert kind="info">{notice}</Alert>}
      {loading ? <Loading /> : error || !data ? <Alert kind="error">{error}</Alert> : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm" data-testid="admin-users">
            <thead className="bg-slate-50 text-left text-slate-500"><tr>
              <th className="px-4 py-2">Email</th><th className="px-4 py-2">Created</th><th className="px-4 py-2">Invested</th>
              <th className="px-4 py-2">KYC</th><th className="px-4 py-2">Status</th><th className="px-4 py-2 text-right">Actions</th>
            </tr></thead>
            <tbody>
              {data.items.length === 0 && <tr><td colSpan={6} className="px-4 py-6 text-center text-slate-500">No users match.</td></tr>}
              {data.items.map((u) => (
                <tr key={u.id} className="border-t border-slate-100">
                  <td className="px-4 py-2">{u.email}{u.role === "admin" && <span className="badge ml-2 bg-brand-100 text-brand-800">admin</span>}{u.totp_enabled && <span className="badge ml-2 bg-green-100 text-green-800" data-testid="totp-pill">2FA</span>}</td>
                  <td className="px-4 py-2">{fmtDay(u.created_at)}</td>
                  <td className="px-4 py-2">{fmtUsdt(u.invested_usdt)}</td>
                  <td className="px-4 py-2"><StatusBadge status={u.kyc_status} /></td>
                  <td className="px-4 py-2"><StatusBadge status={u.suspended ? "suspended" : "active"} />
                    {u.wallet_hold_until && new Date(u.wallet_hold_until).getTime() > Date.now() && (
                      <div className="mt-1 text-xs text-amber-700" data-testid="hold-until">Wallet hold until {fmtDate(u.wallet_hold_until)}</div>
                    )}
                  </td>
                  <td className="space-x-2 whitespace-nowrap px-4 py-2 text-right">
                    {u.suspended
                      ? <button className="btn-secondary !py-1" onClick={() => void unsuspend(u)}>Unsuspend</button>
                      : <button className="btn-secondary !py-1" onClick={() => { setDialogError(null); setPending({ kind: "suspend", user: u }); }}>Suspend</button>}
                    {u.wallet_hold_until && new Date(u.wallet_hold_until).getTime() > Date.now() && (
                      <button className="btn-secondary !py-1" onClick={() => { setDialogError(null); setHoldReason(""); setPending({ kind: "lift_hold", user: u }); }}>Lift wallet hold</button>
                    )}
                    {u.totp_enabled && (
                      <button className="btn-secondary !py-1" onClick={() => { setDialogError(null); setResetCode(""); setPending({ kind: "reset_2fa", user: u }); }}>Reset 2FA</button>
                    )}
                    <button className="btn-danger !py-1" onClick={() => { setDialogError(null); setPending({ kind: "delete", user: u }); }}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {pending && (
        <ConfirmEmailDialog
          title={pending.kind === "delete" ? "Delete this user?" : pending.kind === "lift_hold" ? "Lift the wallet-change hold?"
            : pending.kind === "reset_2fa" ? "Reset two-factor authentication?" : "Suspend this user?"}
          description={pending.kind === "delete"
            ? "Deleting removes their exchange keys, schedules AND transaction history. This cannot be undone."
            : pending.kind === "lift_hold"
              ? "Only after the customer has proved who they are. Lifting the hold lets them send coins to new addresses right away; the customer is emailed and the override is audited with your reason."
              : pending.kind === "reset_2fa"
                ? "Only after the customer has proved who they are. Their authenticator enrolment and recovery codes are removed, they can sign in with the password alone, and they are emailed to set it up again."
                : "A suspended user cannot sign in and none of their schedules execute until unsuspended."}
          email={pending.user.email}
          confirmLabel={pending.kind === "delete" ? "Delete user" : pending.kind === "lift_hold" ? "Lift hold" : pending.kind === "reset_2fa" ? "Reset 2FA" : "Suspend user"}
          danger={pending.kind !== "lift_hold" && pending.kind !== "reset_2fa"}
          busy={busy}
          error={dialogError}
          extra={pending.kind === "lift_hold" ? (
            <div>
              <label className="label" htmlFor="hold-reason">How did the customer prove their identity?</label>
              <input id="hold-reason" className="input" value={holdReason} onChange={(e) => setHoldReason(e.target.value)} placeholder="e.g. video call with ID, ticket #4711" />
            </div>
          ) : pending.kind === "reset_2fa" ? (
            <div>
              <label className="label" htmlFor="reset-2fa-code">Your authenticator code</label>
              <input id="reset-2fa-code" className="input font-mono tracking-widest" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={7}
                     aria-describedby="reset-2fa-code-help" value={resetCode} onChange={(e) => setResetCode(e.target.value)} placeholder="123456" />
              <p id="reset-2fa-code-help" className="mt-1 text-xs text-slate-500">Needed when two-factor is on for your own account.</p>
            </div>
          ) : undefined}
          onConfirm={(typed) => void confirmPending(typed)}
          onCancel={() => { if (!busy) setPending(null); }}
        />
      )}
    </div>
  );
}
