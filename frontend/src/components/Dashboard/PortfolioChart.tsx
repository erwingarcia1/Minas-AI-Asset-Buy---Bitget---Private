import { useCallback, useEffect, useRef, useState } from "react";
import { Area, ComposedChart, Line, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { dashboardAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { EstimatesNote } from "@/components/Dashboard/Analytics";
import { usePortfolioRefresh } from "@/hooks/usePortfolioRefresh";
import type { PortfolioSeries, SeriesBuy, SeriesRange } from "@/types";
import { assetColor } from "@/utils/assetColors";
import { fmtCrypto, fmtUsdt } from "@/utils/format";

export const RANGES: SeriesRange[] = ["1D", "7D", "30D", "90D", "All"];
const RANGE_LABEL: Record<SeriesRange, string> = { "1D": "24 hours", "7D": "7 days", "30D": "30 days", "90D": "90 days", All: "all time" };
const VALUE = "#543477";      // brand-600 (was #1b5cf5 blue before the brand palette)
const INVESTED = "#94a3b8";   // slate-400

const tone = (n?: number | null) => (n == null ? "text-slate-500" : n >= 0 ? "text-green-700" : "text-red-700");
const signed = (n: number) => `${n >= 0 ? "+" : "−"}${fmtUsdt(Math.abs(n))}`;
const signedPct = (n: number) => `${n >= 0 ? "+" : "−"}${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;

export function changeText(s: PortfolioSeries): string {
  if (s.change_usdt == null) return "—";
  const pct = s.change_pct != null ? ` (${signedPct(s.change_pct)})` : "";
  return `${signed(s.change_usdt)}${pct}`;
}

const tickFor = (range: SeriesRange) => (t: number) => {
  const d = new Date(t * 1000);
  if (range === "1D") return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (range === "All") return d.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};
const fullDate = (t: number, range: SeriesRange) =>
  new Date(t * 1000).toLocaleString(undefined, range === "90D" || range === "All"
    ? { dateStyle: "medium" } : { dateStyle: "medium", timeStyle: "short" });

/** Buys grouped onto the bucket they landed in, so markers and the crosshair can list them. */
export function buysByBucket(s: PortfolioSeries): Map<number, SeriesBuy[]> {
  const out = new Map<number, SeriesBuy[]>();
  for (const b of s.buys) {
    const bucket = s.points.find((p) => p.t >= b.t) ?? s.points[s.points.length - 1];
    if (!bucket) continue;
    out.set(bucket.t, [...(out.get(bucket.t) ?? []), b]);
  }
  return out;
}

type Fetch = { data: PortfolioSeries | null; error: string | null; loading: boolean };

/** Portfolio value over time: the filled area is what the holdings were worth
 *  at each point (Bitget candle closes; the last point at the current price
 *  the performance card uses), the dashed step is what had been paid in by
 *  then. Dots mark buys. Refresh policy: usePortfolioRefresh. */
export default function PortfolioChart() {
  const [range, setRange] = useState<SeriesRange>("30D");
  const [state, setState] = useState<Fetch>({ data: null, error: null, loading: true });
  const seq = useRef(0);

  const load = useCallback(async (r: SeriesRange, silent: boolean) => {
    const id = ++seq.current;
    if (!silent) setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await dashboardAPI.portfolioSeries(r);
      if (id === seq.current) setState({ data, error: null, loading: false });
    } catch (e) {
      if (id === seq.current) setState((s) => ({ data: silent ? s.data : null, error: errorMessage(e), loading: false }));
    }
  }, []);
  useEffect(() => { void load(range, false); }, [range, load]);
  const refresh = useCallback(() => { void load(range, true); }, [range, load]);
  usePortfolioRefresh(refresh);

  const { data, error, loading } = state;
  const rangeControl = (
    <div role="group" aria-label="Range" className="inline-flex rounded-md border border-slate-200 text-xs">
      {RANGES.map((r, i) => (
        <button key={r} type="button" aria-pressed={range === r} onClick={() => setRange(r)}
                className={`px-2 py-1 ${i === 0 ? "rounded-l-md" : ""} ${i === RANGES.length - 1 ? "rounded-r-md" : ""} ${range === r ? "bg-slate-800 text-white" : "text-slate-600 hover:bg-slate-50"}`}>
          {r}
        </button>
      ))}
    </div>
  );

  return (
    <section className="card" aria-label="Portfolio value">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-semibold">Portfolio value</h2>
          {data && (
            <p className="mt-1" data-testid="portfolio-headline">
              <span className="text-2xl font-semibold text-slate-900" data-testid="portfolio-current">
                {data.current_value != null ? fmtUsdt(data.current_value) : "—"}
              </span>
              <span className={`ml-2 text-sm font-medium ${tone(data.change_usdt)}`} data-testid="portfolio-change"
                    title="Change in value over the range, excluding what buys made in the range added. Not time-weighted.">
                {changeText(data)} · {range}
              </span>
            </p>
          )}
        </div>
        {rangeControl}
      </div>

      {error && (
        <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900" role="status" data-testid="portfolio-unavailable">
          Portfolio chart unavailable right now: {error} Holdings and performance figures are unaffected.{" "}
          <button type="button" className="underline" onClick={() => void load(range, false)}>Retry</button>
        </div>
      )}
      {!error && loading && !data && <p className="mt-3 text-sm text-slate-500" role="status">Loading portfolio value…</p>}
      {!error && data && data.buys.length === 0 && data.points.length === 0 && (
        <p className="mt-3 text-sm text-slate-500">No completed purchases yet.</p>
      )}
      {!error && data && data.points.length > 0 && data.points.length < 2 && (
        <p className="mt-3 text-sm text-slate-500">Not enough history for this range yet.</p>
      )}
      {!error && data && data.points.length >= 2 && <Chart series={data} range={range} />}
      {data && data.unpriced.length > 0 && (
        <p className="mt-2 text-xs text-amber-700">No current price for {data.unpriced.join(", ")}; the last point leaves it out.</p>
      )}
      <div className="mt-2"><EstimatesNote /></div>
    </section>
  );
}

function Chart({ series, range }: { series: PortfolioSeries; range: SeriesRange }) {
  const buys = buysByBucket(series);
  const first = series.points[0];
  const last = series.points[series.points.length - 1];
  const summary = `Portfolio value ${last.value != null ? fmtUsdt(last.value) : "unknown"}, ${series.change_usdt != null ? `${changeText(series)} over the last ${RANGE_LABEL[range]} excluding buys in the range` : "no change figure for this range"}; ${fmtUsdt(series.invested)} invested; ${series.buys.length} ${series.buys.length === 1 ? "buy" : "buys"} in range.`;
  return (
    <div className="mt-3">
      <p className="sr-only" data-testid="portfolio-summary">{summary}</p>
      <div className="h-60 touch-pan-y" data-testid="portfolio-chart" role="img" aria-label={summary}>
        <ResponsiveContainer initialDimension={{ width: 600, height: 240 }}>
          <ComposedChart data={series.points} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
            <defs>
              <linearGradient id="portfolio-value-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={VALUE} stopOpacity={0.35} />
                <stop offset="100%" stopColor={VALUE} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <XAxis dataKey="t" type="number" domain={[first.t, last.t]} tickFormatter={tickFor(range)} minTickGap={48}
                   axisLine={false} tickLine={false} tick={{ fontSize: 11, fill: "#94a3b8" }} />
            <YAxis hide domain={["auto", "auto"]} />
            <Tooltip cursor={{ stroke: "#64748b", strokeDasharray: "3 3" }} isAnimationActive={false}
                     content={({ active, payload }) => {
                       const p = active && payload && payload.length ? (payload[0].payload as { t: number; value: number; invested: number }) : null;
                       if (!p) return null;
                       const here = buys.get(p.t) ?? [];
                       return (
                         <div className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs shadow-sm">
                           <p className="text-slate-500">{fullDate(p.t, range)}</p>
                           <p><span className="font-medium text-slate-900">{fmtUsdt(p.value)}</span> <span className="text-slate-500">value</span></p>
                           <p><span className="font-medium text-slate-700">{fmtUsdt(p.invested)}</span> <span className="text-slate-500">invested</span></p>
                           {here.map((b, i) => (
                             <p key={i} style={{ color: assetColor(b.crypto) }}>Bought {fmtCrypto(b.amount, b.crypto)} for {fmtUsdt(b.cost_usd)}</p>
                           ))}
                         </div>
                       );
                     }} />
            <Area type="monotone" dataKey="value" stroke={VALUE} strokeWidth={2} fill="url(#portfolio-value-fill)" isAnimationActive={false} dot={false} activeDot={{ r: 4 }} />
            <Line type="stepAfter" dataKey="invested" stroke={INVESTED} strokeWidth={1.5} strokeDasharray="4 3" dot={false} activeDot={false} isAnimationActive={false} />
            {[...buys.entries()].map(([t, list]) => {
              const point = series.points.find((p) => p.t === t);
              return point ? <ReferenceDot key={t} x={t} y={point.value} r={4} fill={assetColor(list[0].crypto)} stroke="#fff" strokeWidth={1.5} /> : null;
            })}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500" aria-hidden>
        <span><span className="inline-block h-2 w-3 rounded-sm align-middle" style={{ background: VALUE }} /> Value</span>
        <span><span className="inline-block w-3 border-t-2 border-dashed align-middle" style={{ borderColor: INVESTED }} /> Invested</span>
        <span><span className="inline-block h-2 w-2 rounded-full bg-slate-700 align-middle" /> Buy</span>
        <span>Change excludes buys in the range</span>
        {series.carried_forward > 0 && <span>{series.carried_forward} {series.carried_forward === 1 ? "bucket" : "buckets"} priced at the previous close (no candle)</span>}
      </div>
    </div>
  );
}
