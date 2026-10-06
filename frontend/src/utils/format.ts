export const fmtUsdt = (n: number | null | undefined) =>
  n == null ? "—" : `${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`;

/** A price in USDT with exactly the decimals Bitget quotes the pair in (BTC 2, XRP 4, XLM 5),
 *  so sub-dollar coins keep their significant figures: 0.20945, not 0.21. */
export const fmtPrice = (n: number | null | undefined, precision = 8) =>
  n == null ? "—" : `${n.toLocaleString(undefined, { minimumFractionDigits: precision, maximumFractionDigits: precision })} USDT`;

/** Crypto amounts: at most 8 decimals, trailing zeros trimmed, never exponent notation ("0.00076179", "0.5", "0"). */
export const fmtAmount = (n: number, digits = 8) => {
  const fixed = n.toFixed(digits);
  return fixed.includes(".") ? fixed.replace(/0+$/, "").replace(/\.$/, "") : fixed;
};
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
