import { Link } from "react-router-dom";
import { BetaTicker } from "@/components/Common/BetaTicker";
import { CoinLogo } from "@/components/Common/CoinLogo";
import { LOGO_ENTRIES } from "@/utils/logoSources";

/** Generated from public/logos/LOGO_SOURCES.md: one row per supported coin. */
export default function LogoCredits() {
  return (
    <><BetaTicker /><main className="mx-auto max-w-3xl px-4 py-16">
      <h1 className="text-2xl font-semibold text-brand-ink">Logo credits</h1>
      <p className="mt-4 text-slate-700">
        Coin logos belong to their projects and appear here only to identify the asset. Coins without a licensed mark show a lettered circle.
      </p>
      <div className="mt-6 overflow-x-auto">
        <table className="w-full text-sm" data-testid="logo-credits">
          <thead className="text-left text-slate-500"><tr><th className="py-1 pr-3">Coin</th><th className="pr-3">Source</th><th className="pr-3">Licence or terms</th></tr></thead>
          <tbody>
            {LOGO_ENTRIES.map((e) => (
              <tr key={e.symbol} className="border-t border-slate-100 align-top" data-testid={`credit-${e.symbol}`}>
                <td className="py-2 pr-3 font-medium"><span className="inline-flex items-center gap-2"><CoinLogo symbol={e.symbol} size={24} />{e.symbol}</span></td>
                <td className="py-2 pr-3 text-slate-700">
                  {e.sourceUrl ? <a className="text-brand-600 underline" href={e.sourceUrl} target="_blank" rel="noopener noreferrer">{e.source}</a> : e.source}
                  {e.file && <span className="block text-xs text-slate-400">{e.file}</span>}
                </td>
                <td className="py-2 text-slate-700">
                  {e.licence || "—"}
                  {e.notice && <p className="mt-1 text-xs text-slate-500" data-testid={`notice-${e.symbol}`}>{e.notice}</p>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-6 text-sm"><Link className="text-brand-600 underline" to="/">Back to AutoBuy</Link></p>
    </main></>
  );
}
