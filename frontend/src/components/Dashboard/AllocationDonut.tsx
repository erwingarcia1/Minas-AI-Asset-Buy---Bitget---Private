import { useMemo, useState } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import type { PerformanceHolding } from "@/types";
import { MAX_SLICES, assetColor } from "@/utils/assetColors";
import { fmtCrypto, fmtUsdt } from "@/utils/format";

type Mode = "value" | "cost";

export interface Slice {
  name: string;          // asset symbol, or "Other"
  amount: number;        // quantity acquired (net of the coin-denominated trading fee)
  usdt: number;          // current market value or cost, per the mode
  pct: number;           // share of the total, per the mode
  members?: string[];    // the assets grouped into "Other"
}

/** Slices for the donut: one per asset, biggest first; beyond MAX_SLICES the
 *  rest collapse into "Other". Quantities are the acquired figures; the USDT
 *  figure is the market value (mode "value") or the cost paid (mode "cost"). */
export function buildSlices(holdings: PerformanceHolding[], mode: Mode): { slices: Slice[]; total: number } {
  const rows = holdings
    .map((h) => ({ name: h.crypto, amount: h.total_amount, usdt: mode === "value" ? (h.market_value ?? 0) : h.invested_usdt }))
    .filter((r) => r.usdt > 0)
    .sort((a, b) => b.usdt - a.usdt);
  const total = rows.reduce((s, r) => s + r.usdt, 0);
  const pct = (usdt: number) => (total > 0 ? (usdt / total) * 100 : 0);
  if (rows.length <= MAX_SLICES) return { total, slices: rows.map((r) => ({ ...r, pct: pct(r.usdt) })) };
  const kept = rows.slice(0, MAX_SLICES - 1);
  const rest = rows.slice(MAX_SLICES - 1);
  const other: Slice = {
    name: "Other", amount: 0, usdt: rest.reduce((s, r) => s + r.usdt, 0), pct: 0, members: rest.map((r) => r.name),
  };
  other.pct = pct(other.usdt);
  return { total, slices: [...kept.map((r) => ({ ...r, pct: pct(r.usdt) })), other] };
}

const pctText = (n: number) => `${n.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;

function SliceDetails({ slice, mode }: { slice: Slice; mode: Mode }) {
  return (
    <>
      <p className="text-sm font-semibold" style={{ color: assetColor(slice.name) }}>{slice.name}</p>
      {slice.members
        ? <p className="text-xs text-slate-500">{slice.members.join(", ")}</p>
        : <p className="text-xs text-slate-500">{fmtCrypto(slice.amount, slice.name)}</p>}
      <p className="text-sm font-medium text-slate-900">{fmtUsdt(slice.usdt)}</p>
      <p className="text-xs text-slate-500">{pctText(slice.pct)} of {mode === "value" ? "value" : "cost"}</p>
    </>
  );
}

/** Allocation across held assets. Default: share of current market value;
 *  the toggle switches to share of cost paid. Hover or focus a slice or a
 *  legend row for that asset's quantity, USDT figure and share. */
export default function AllocationDonut({ holdings, priceAvailable, totalMarketValue }: {
  holdings: PerformanceHolding[]; priceAvailable: boolean; totalMarketValue?: number | null;
}) {
  const [mode, setMode] = useState<Mode>(priceAvailable ? "value" : "cost");
  const [active, setActive] = useState<number | null>(null);
  const effectiveMode: Mode = priceAvailable ? mode : "cost";
  const { slices, total } = useMemo(() => buildSlices(holdings, effectiveMode), [holdings, effectiveMode]);
  // the centre total is the same figure the performance card shows as market value
  const centreTotal = effectiveMode === "value" && totalMarketValue != null ? totalMarketValue : total;
  const activeSlice = active != null ? slices[active] : null;
  const label = effectiveMode === "value" ? "Allocation by current value" : "Allocation by cost";

  if (slices.length === 0) {
    return (
      <div data-testid="allocation-donut">
        <h3 className="text-sm font-medium text-slate-700">Allocation</h3>
        <p className="mt-2 text-sm text-slate-500">No completed purchases yet.</p>
      </div>
    );
  }

  return (
    <div data-testid="allocation-donut">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-slate-700" id="allocation-label">{label}</h3>
        <div role="group" aria-label="Allocation basis" className="inline-flex rounded-md border border-slate-200 text-xs">
          <button type="button" aria-pressed={effectiveMode === "value"} disabled={!priceAvailable}
                  title={priceAvailable ? undefined : "Live prices are unavailable right now"}
                  className={`px-2 py-1 rounded-l-md ${effectiveMode === "value" ? "bg-slate-800 text-white" : "text-slate-600 hover:bg-slate-50 disabled:text-slate-300"}`}
                  onClick={() => setMode("value")}>Current value</button>
          <button type="button" aria-pressed={effectiveMode === "cost"}
                  className={`px-2 py-1 rounded-r-md ${effectiveMode === "cost" ? "bg-slate-800 text-white" : "text-slate-600 hover:bg-slate-50"}`}
                  onClick={() => setMode("cost")}>Cost</button>
        </div>
      </div>
      <div className="mt-2 flex flex-col items-center gap-4 sm:flex-row">
        <div className="relative h-52 w-52 shrink-0" role="img" aria-labelledby="allocation-label" aria-describedby="allocation-legend">
          <ResponsiveContainer initialDimension={{ width: 208, height: 208 }}>
            <PieChart>
              <Pie data={slices} dataKey="usdt" nameKey="name" innerRadius={62} outerRadius={94} paddingAngle={slices.length > 1 ? 2 : 0}
                   stroke="#fff" strokeWidth={1} isAnimationActive={false}
                   onMouseEnter={(_, i) => setActive(i)} onMouseLeave={() => setActive(null)}>
                {slices.map((s, i) => (
                  <Cell key={s.name} fill={assetColor(s.name)} opacity={active == null || active === i ? 1 : 0.45} />
                ))}
              </Pie>
              <Tooltip content={() => null} />
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center px-8 text-center" data-testid="donut-centre">
            {activeSlice ? <SliceDetails slice={activeSlice} mode={effectiveMode} /> : (
              <>
                <p className="text-xs text-slate-500">{effectiveMode === "value" ? "Market value" : "Total cost"}</p>
                <p className="text-base font-semibold text-slate-900" data-testid="donut-total">{fmtUsdt(centreTotal)}</p>
              </>
            )}
          </div>
        </div>
        <ul id="allocation-legend" className="w-full space-y-1 text-sm" aria-label="Allocation legend">
          {slices.map((s, i) => (
            <li key={s.name}>
              <button type="button"
                      className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${active === i ? "bg-slate-50" : ""}`}
                      onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)}
                      onFocus={() => setActive(i)} onBlur={() => setActive(null)}
                      aria-label={`${s.name}: ${pctText(s.pct)}, ${fmtUsdt(s.usdt)}${s.members ? ` (${s.members.join(", ")})` : `, ${fmtCrypto(s.amount, s.name)}`}`}>
                <span aria-hidden className="h-3 w-3 shrink-0 rounded-sm" style={{ background: assetColor(s.name) }} />
                <span className="w-14 font-medium">{s.name}</span>
                <span className="w-14 text-slate-600">{pctText(s.pct)}</span>
                <span className="ml-auto whitespace-nowrap text-slate-600">{fmtUsdt(s.usdt)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
