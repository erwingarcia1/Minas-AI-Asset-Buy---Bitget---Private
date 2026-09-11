import { useEffect } from "react";
import { adminAPI } from "@/api/endpoints";
import { Alert, Loading } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";

const TONES: Record<string, string> = {
  ok: "bg-green-100 text-green-800",
  warning: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
};

/** Live snapshot of the docs/ALERTING.md conditions; refreshes every 30 s. */
export default function AlertMonitor() {
  const { data, loading, error, reload } = useAsync(() => adminAPI.alerts());

  useEffect(() => {
    const timer = window.setInterval(() => void reload(), 30_000);
    return () => window.clearInterval(timer);
  }, [reload]);

  if (loading && !data) return <Loading label="Loading alerts…" />;
  if (error || !data) return <Alert kind="error">{error}</Alert>;
  return (
    <ul className="space-y-2" data-testid="admin-alerts">
      {data.map((a) => (
        <li key={a.name} className="card flex items-center gap-3 p-3">
          <span className={`badge ${TONES[a.severity] ?? "bg-slate-100 text-slate-700"}`}>{a.severity}</span>
          <div className="flex-1">
            <div className="text-sm font-medium">{a.name} = {a.value}</div>
            <div className="text-xs text-slate-500">{a.detail}</div>
          </div>
        </li>
      ))}
    </ul>
  );
}
