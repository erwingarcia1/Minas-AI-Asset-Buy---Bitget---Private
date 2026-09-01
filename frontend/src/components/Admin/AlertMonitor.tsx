import { useEffect, useState } from "react";
import { admin } from "../../services/api";
import type { AdminAlert } from "../../services/api";

const TONES: Record<string, string> = {
  ok: "bg-teal-50 text-teal-700",
  warning: "bg-amber-50 text-amber-700",
  critical: "bg-red-50 text-red-700",
};

export default function AlertMonitor() {
  const [alerts, setAlerts] = useState<AdminAlert[]>([]);
  useEffect(() => {
    const load = () => admin.alerts().then(setAlerts).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <ul className="space-y-2">
      {alerts.map((a) => (
        <li key={a.name} className="flex items-center gap-3 rounded-lg bg-white p-3 shadow-sm">
          <span className={`rounded-full px-2 py-0.5 text-xs ${TONES[a.severity]}`}>
            {a.severity}
          </span>
          <div className="flex-1">
            <div className="text-sm font-medium">{a.name} = {a.value}</div>
            <div className="text-xs text-gray-500">{a.detail}</div>
          </div>
        </li>
      ))}
      {alerts.length === 0 && <li className="text-sm text-gray-500">Loading alerts…</li>}
    </ul>
  );
}
