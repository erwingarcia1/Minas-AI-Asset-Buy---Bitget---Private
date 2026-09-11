import { useState } from "react";
import { adminAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import type { AdminUserRow } from "@/types";
import { fmtDay, fmtUsdt } from "@/utils/format";
import { ConfirmEmailDialog } from "./ConfirmEmailDialog";

type Pending = { kind: "suspend" | "delete"; user: AdminUserRow } | null;

export default function UserManagement() {
  const [query, setQuery] = useState("");
  const { data, loading, error, reload } = useAsync(() => adminAPI.users(query), [query]);
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function unsuspend(u: AdminUserRow) {
    setNotice(null);
    try { await adminAPI.unsuspend(u.id); setNotice(`${u.email} unsuspended.`); await reload(); }
    catch (err) { setNotice(errorMessage(err)); }
  }

  async function confirmPending(typed: string) {
    if (!pending) return;
    setBusy(true); setDialogError(null);
    try {
      if (pending.kind === "delete") await adminAPI.removeUser(pending.user.id, typed);
      else await adminAPI.suspend(pending.user.id);
      setNotice(pending.kind === "delete" ? `${pending.user.email} deleted.` : `${pending.user.email} suspended.`);
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
                  <td className="px-4 py-2">{u.email}{u.role === "admin" && <span className="badge ml-2 bg-brand-100 text-brand-800">admin</span>}</td>
                  <td className="px-4 py-2">{fmtDay(u.created_at)}</td>
                  <td className="px-4 py-2">{fmtUsdt(u.invested_usdt)}</td>
                  <td className="px-4 py-2"><StatusBadge status={u.kyc_status} /></td>
                  <td className="px-4 py-2"><StatusBadge status={u.suspended ? "suspended" : "active"} /></td>
                  <td className="space-x-2 whitespace-nowrap px-4 py-2 text-right">
                    {u.suspended
                      ? <button className="btn-secondary !py-1" onClick={() => void unsuspend(u)}>Unsuspend</button>
                      : <button className="btn-secondary !py-1" onClick={() => { setDialogError(null); setPending({ kind: "suspend", user: u }); }}>Suspend</button>}
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
          title={pending.kind === "delete" ? "Delete this user?" : "Suspend this user?"}
          description={pending.kind === "delete"
            ? "Deleting removes their exchange keys, schedules AND transaction history. This cannot be undone."
            : "A suspended user cannot sign in and none of their schedules execute until unsuspended."}
          email={pending.user.email}
          confirmLabel={pending.kind === "delete" ? "Delete user" : "Suspend user"}
          busy={busy}
          error={dialogError}
          onConfirm={(typed) => void confirmPending(typed)}
          onCancel={() => { if (!busy) setPending(null); }}
        />
      )}
    </div>
  );
}
