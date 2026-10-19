export const fmtUsdt = (n: number | null | undefined) =>
  n == null ? "—" : `${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`;

/** A price in USDT with exactly the decimals Bitget quotes the pair in (BTC 2, XRP 4, XLM 5),
 *  so sub-dollar coins keep their significant figures: 0.20945, not 0.21. */
/** Prices for humans: at least 4 significant digits, never fewer than 2 decimals, thousands
 *  separators ("84,487.36 USDT", "0.09179 USDT", "0.000004181 USDT"). Full precision stays in the API. */
export const fmtPrice = (n: number | null | undefined) =>
  n == null ? "—" : `${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: priceDecimals(n) })} USDT`;

/** Crypto amounts: at most 8 decimals, trailing zeros trimmed, never exponent notation ("0.00076179", "0.5", "0"). */
/** Coin amounts for humans: thousands separators; at or above 1,000 coins at most 2 decimals
 *  ("2,389,380.23"), below that at most `digits` decimals, trailing zeros trimmed ("0.00076179"). */
export const fmtAmount = (n: number, digits = 8) =>
  n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: Math.abs(n) >= 1000 ? 2 : digits });

/** Decimals that show at least 4 significant digits, never fewer than 2, at most 10. */
const priceDecimals = (n: number) => (n === 0 ? 2 : Math.max(2, Math.min(10, 3 - Math.floor(Math.log10(Math.abs(n))))));
export const fmtCrypto = (n: number | null | undefined, symbol = "") =>
  n == null ? "—" : `${fmtAmount(n)} ${symbol}`.trim();

export const fmtDate = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

export const fmtDay = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }) : "—";

export const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** Scheduled run times for customers: Eastern Time first, the UTC clock in brackets
 *  ("Saturday, Oct 11 at 8:16 PM ET (00:16 UTC)"). No per-user time zone is stored yet. */
export const DISPLAY_TZ = "America/New_York";
export const fmtRunTime = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(iso);
  const day = d.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric", timeZone: DISPLAY_TZ });
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: DISPLAY_TZ });
  const utc = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  return `${day} at ${time} ET (${utc} UTC)`;
};

/** Times that come from the Guardian engine: a fixed UTC clock that says so ("2026-10-19 14:03 UTC").
 *  A stamp without a zone is taken as UTC, which is what the engine writes. */
export const fmtUtc = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
};
