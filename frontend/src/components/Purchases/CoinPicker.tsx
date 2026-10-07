import { CoinLogo } from "@/components/Common/CoinLogo";
import { useEffect, useState } from "react";
import { COIN_CATEGORIES, categoryOf, type CoinCategory } from "@/utils/exchangeNetworks";

/** Category tabs first, then the coins in that category as buttons with name and symbol.
 *  Mobile-first: tabs wrap, buttons fill a responsive grid. The groups come from
 *  /api/public/meta when the caller has them, else the built-in copy of the registry. */
export default function CoinPicker({ value, onChange, groups = COIN_CATEGORIES, disabled = false }:
  { value: string; onChange: (symbol: string) => void; groups?: CoinCategory[]; disabled?: boolean }) {
  const [tab, setTab] = useState<string>(() => categoryOf(value, groups)?.category ?? groups[0]?.category ?? "main");
  useEffect(() => { const g = categoryOf(value, groups); if (g && g.category !== tab) setTab(g.category); }, [value, groups]);   // eslint-disable-line react-hooks/exhaustive-deps
  const group = groups.find((g) => g.category === tab) ?? groups[0];
  return (
    <div>
      <span className="label" id="pf-category-label">Category</span>
      <div role="tablist" aria-labelledby="pf-category-label" className="flex flex-wrap gap-2">
        {groups.map((g) => (
          <button key={g.category} type="button" role="tab" aria-selected={g.category === tab} disabled={disabled}
                  className={`rounded-full border px-3 py-1.5 text-sm font-medium ${g.category === tab ? "border-brand-600 bg-brand-600 text-white" : "border-slate-200 bg-white text-slate-700 hover:border-slate-300"}`}
                  onClick={() => setTab(g.category)}>{g.label}</button>
        ))}
      </div>
      <span className="label mt-3 block" id="pf-crypto-label">Coin</span>
      <div role="group" aria-labelledby="pf-crypto-label" className="grid grid-cols-3 gap-2 sm:grid-cols-4" data-testid="coin-picker">
        {group?.coins.map((c) => (
          <button key={c.symbol} type="button" aria-pressed={c.symbol === value} disabled={disabled}
                  className={`rounded-lg border p-2 text-left ${c.symbol === value ? "border-brand-600 ring-2 ring-brand-100" : "border-slate-200 bg-white hover:border-slate-300"}`}
                  onClick={() => onChange(c.symbol)}>
            <span className="flex items-center gap-2 text-sm font-semibold text-slate-900"><CoinLogo symbol={c.symbol} size={32} />{c.symbol}</span>
            <span className="block text-xs text-slate-500">{c.name}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
