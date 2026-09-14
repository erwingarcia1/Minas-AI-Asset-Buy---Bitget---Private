import { useState } from "react";
import { Navigate } from "react-router-dom";
import { adminAPI } from "@/api/endpoints";
import AlertMonitor from "@/components/Admin/AlertMonitor";
import TransactionLog from "@/components/Admin/TransactionLog";
import ReviewQueue from "@/components/Admin/ReviewQueue";
import UserManagement from "@/components/Admin/UserManagement";
import { Alert, Loading } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { useAuth } from "@/hooks/useAuth";
import { fmtUsdt } from "@/utils/format";

const TABS = ["Overview", "Users", "Transactions", "Review", "Alerts"] as const;
type Tab = (typeof TABS)[number];

/**
 * Admin console. The role check here only decides what to render; every
 * /api/admin/* route enforces role=admin on the server and answers 403 otherwise.
 */
export default function Admin() {
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>("Overview");
  if (!user) return <Loading />;
  if (user.role !== "admin") return <Navigate to="/dashboard" replace />;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Admin</h1>
      <nav className="flex flex-wrap gap-2" aria-label="Admin sections">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} aria-current={tab === t ? "page" : undefined}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t ? "bg-brand-600 text-white" : "bg-white text-slate-700 shadow-sm hover:bg-slate-50"}`}>
            {t}
          </button>
        ))}
      </nav>
      {tab === "Overview" && <Overview />}
      {tab === "Users" && <UserManagement />}
      {tab === "Transactions" && <TransactionLog />}
      {tab === "Review" && <ReviewQueue />}
      {tab === "Alerts" && <AlertMonitor />}
    </div>
  );
}

function Overview() {
  const { data: stats, loading, error } = useAsync(() => adminAPI.stats());
  if (loading) return <Loading />;
  if (error || !stats) return <Alert kind="error">{error}</Alert>;
  const cells: [string, React.ReactNode][] = [
    ["Users", stats.total_users], ["Verified", stats.verified_users], ["Suspended", stats.suspended_users],
    ["Signups 7d", stats.signups_7d], ["Invested", fmtUsdt(stats.total_invested_usdt)], ["Volume 7d", fmtUsdt(stats.volume_7d_usdt)],
    ["Transactions", stats.total_transactions], ["Failed 24h", stats.failed_transactions_24h],
  ];
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" data-testid="admin-overview">
      {cells.map(([label, value]) => (
        <div key={label} className="card">
          <p className="text-sm text-slate-500">{label}</p>
          <p className="mt-1 text-2xl font-semibold text-slate-900">{value}</p>
        </div>
      ))}
    </div>
  );
}
