/** One colour per asset, used by the allocation donut, its legend and the
 *  performance table so an asset looks the same everywhere on the dashboard.
 *  "Other" groups the smallest positions when more than MAX_SLICES are held. */
export const ASSET_COLORS: Record<string, string> = {
  BTC: "#f7931a",
  ETH: "#5b6ee1",
  USDC: "#2775ca",
  SOL: "#db2777",   // pink; the previous violet #7c3aed clashed with the brand purple (#543477)
  XRP: "#0e7490",
  XLM: "#0f9d75",
  DOGE: "#7c4a1e",   // chocolate brown: away from the amber "paused" badge
  SHIB: "#c026d3",   // fuchsia (provisional: judged next to SOL's pink)
  PEPE: "#65a30d",   // lime: away from the green "active" badge
  Other: "#94a3b8",
};
const FALLBACKS = ["#d97706", "#db2777", "#4f46e5", "#0891b2", "#65a30d", "#ca8a04"];   // no purples: they would read as the brand colour

export const MAX_SLICES = 6;

export function assetColor(symbol: string): string {
  if (ASSET_COLORS[symbol]) return ASSET_COLORS[symbol];
  let h = 0;
  for (const ch of symbol) h = (h * 31 + ch.charCodeAt(0)) % FALLBACKS.length;
  return FALLBACKS[h];
}
