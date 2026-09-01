import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import AlertMonitor from "../components/Admin/AlertMonitor";
import TransactionLog from "../components/Admin/TransactionLog";
import UserManagement from "../components/Admin/UserManagement";
import Loading from "../components/common/Loading";
import { admin, user } from "../services/api";
import type { AdminStats } from "../services/api";

const TABS = ["Overview", "Users", "Transactions", "Alerts"] as const;

export default function AdminPage() {
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Overview");
  const [stats, setStats] = useState<AdminStats | null>(null);

  useEffect(() => {
    user.getProfile().then((p) => setAllowed(p.role === "admin")).catch(() => setAllowed(false));
  }, []);
  useEffect(() => {
    if (allowed) admin.stats().then(setStats).catch(() => undefined);
  }, [allowed]);

  if (allowed === null) return <Loading />;
  if (!allowed) return <Navigate to="/dashboard" replace />;

  return (
    <div className="space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Admin</h1>
      <nav className="flex gap-2">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
                  className={`rounded px-3 py-1.5 text-sm ${
                    tab === t ? "bg-blue-600 text-white" : "bg-white shadow-sm"}`}>
            {t}
          </button>
        ))}
      </nav>
      {tab === "Overview" && stats && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[["Users", stats.total_users], ["Verified", stats.verified_users],
            ["Suspended", stats.suspended_users], ["Signups 7d", stats.signups_7d],
            ["Invested", `$${stats.total_invested_usdt.toLocaleString()}`],
            ["Volume 7d", `$${stats.volume_7d_usdt.toLocaleString()}`],
            ["Transactions", stats.total_transactions],
            ["Failed 24h", stats.failed_transactions_24h]].map(([label, value]) => (
            <div key={String(label)} className="rounded-lg bg-white p-4 shadow-sm">
              <div className="text-xs uppercase text-gray-500">{label}</div>
              <div className="mt-1 text-xl font-semibold">{value}</div>
            </div>
          ))}
        </div>
      )}
      {tab === "Users" && <UserManagement />}
      {tab === "Transactions" && <TransactionLog />}
      {tab === "Alerts" && <AlertMonitor />}
    </div>
  );
}
