import { useCallback, useEffect, useState } from "react";
import { admin } from "../../services/api";
import type { AdminUserRow } from "../../services/api";

export default function UserManagement() {
  const [rows, setRows] = useState<AdminUserRow[]>([]);
  const [query, setQuery] = useState("");
  const [total, setTotal] = useState(0);

  const load = useCallback(() => {
    admin.users(query).then((r) => { setRows(r.items); setTotal(r.total); });
  }, [query]);
  useEffect(load, [load]);

  async function toggleSuspend(u: AdminUserRow) {
    await (u.suspended ? admin.unsuspend(u.id) : admin.suspend(u.id));
    load();
  }

  async function remove(u: AdminUserRow) {
    const confirm = window.prompt(
      `Deleting ${u.email} removes their keys, schedules AND transaction history.\n` +
      "Type the email exactly to confirm:");
    if (confirm == null) return;
    try {
      await admin.removeUser(u.id, confirm);
      load();
    } catch {
      window.alert("Confirmation did not match — nothing deleted.");
    }
  }

  return (
    <div className="space-y-3">
      <input value={query} onChange={(e) => setQuery(e.target.value)}
             placeholder="Search by email…" aria-label="Search users"
             className="w-full max-w-sm rounded border p-2 text-sm" />
      <p className="text-xs text-gray-500">{total} user(s)</p>
      <div className="overflow-x-auto rounded-lg bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-gray-500">
            <tr><th className="p-3">Email</th><th>Created</th><th>Invested</th>
                <th>KYC</th><th>Status</th><th className="text-right">Actions</th></tr>
          </thead>
          <tbody>
            {rows.map((u) => (
              <tr key={u.id} className="border-t">
                <td className="p-3">{u.email}{u.role === "admin" &&
                  <span className="ml-1 rounded bg-blue-50 px-1 text-xs text-blue-700">admin</span>}</td>
                <td>{new Date(u.created_at).toLocaleDateString()}</td>
                <td>${u.invested_usdt.toLocaleString()}</td>
                <td>{u.kyc_status}</td>
                <td>{u.suspended
                  ? <span className="rounded-full bg-red-50 px-2 text-xs text-red-700">suspended</span>
                  : <span className="rounded-full bg-teal-50 px-2 text-xs text-teal-700">active</span>}</td>
                <td className="p-3 text-right">
                  <div className="flex justify-end gap-2 text-xs">
                    <button onClick={() => toggleSuspend(u)} className="text-amber-600">
                      {u.suspended ? "Unsuspend" : "Suspend"}
                    </button>
                    <button onClick={() => remove(u)} className="text-red-600">Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
