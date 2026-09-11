import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Timeline } from "@/types";
import { fmtUsdt } from "@/utils/format";

/** Cumulative USDT invested over the timeline window (default: last 30 days). */
export default function InvestmentTimeline({ timeline }: { timeline: Timeline }) {
  let running = 0;
  const cumulative = timeline.timeline.map((p) => ({ date: p.date.slice(5), invested: (running += p.total_usdt) }));
  const total = timeline.timeline.reduce((s, p) => s + p.total_usdt, 0);

  return (
    <section className="card" aria-label="Investment timeline">
      <div className="flex items-baseline justify-between">
        <h2 className="font-semibold">Invested — last {timeline.days} days</h2>
        <span className="text-sm text-slate-500" data-testid="timeline-total">{fmtUsdt(total)}</span>
      </div>
      {cumulative.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500">No completed purchases in this window.</p>
      ) : (
        <div className="mt-3 h-56" data-testid="timeline-chart">
          <ResponsiveContainer>
            <LineChart data={cumulative}>
              <XAxis dataKey="date" hide />
              <YAxis width={80} tickFormatter={(v) => fmtUsdt(Number(v))} />
              <Tooltip formatter={(v) => fmtUsdt(Number(v))} />
              <Line type="monotone" dataKey="invested" stroke="#1b5cf5" dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
