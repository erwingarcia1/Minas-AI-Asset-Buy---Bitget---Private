import { Link } from "react-router-dom";
import { autoBuyAPI } from "@/api/endpoints";
import { Alert } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { capitalize, fmtRunTime, fmtUsdt } from "@/utils/format";

/** The buys AutoBuy will make next, shown where customers look for them:
 *  above the history they turn into. Active schedules only — a paused or
 *  stopped purchase has no upcoming run. */
export default function UpcomingSchedules() {
  const { data, loading, error } = useAsync(() => autoBuyAPI.list());
  if (loading) return null;
  if (error) return <Alert kind="error">{error}</Alert>;
  const upcoming = (data ?? [])
    .filter((p) => p.status === "active" && p.next_execution)
    .sort((a, b) => (a.next_execution! < b.next_execution! ? -1 : 1));
  return (
    <section className="card" aria-label="Upcoming buys" data-testid="upcoming-schedules">
      <div className="flex items-baseline justify-between">
        <h2 className="font-semibold">Upcoming buys</h2>
        <Link to="/purchases" className="text-sm text-brand-600 underline">Manage schedules</Link>
      </div>
      {upcoming.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500">Nothing scheduled. <Link to="/purchases" className="text-brand-600 underline">Create a schedule</Link> and its next buy will show here.</p>
      ) : (
        <ul className="mt-3 divide-y divide-slate-100 text-sm">
          {upcoming.map((p) => (
            <li key={p.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-2" data-testid="upcoming-row">
              <span>
                <span className="font-medium">{p.crypto_symbol}</span> · {fmtUsdt(p.amount_usd)} · {p.frequency === "one_time" ? "One-time" : capitalize(p.frequency)}
              </span>
              <span className="text-slate-500">{fmtRunTime(p.next_execution)}</span>
            </li>
          ))}
        </ul>
      )}
      {upcoming.length > 0 && (
        <p className="mt-3 text-xs text-slate-400">These run automatically. Once a buy completes it appears in the list below.</p>
      )}
    </section>
  );
}
