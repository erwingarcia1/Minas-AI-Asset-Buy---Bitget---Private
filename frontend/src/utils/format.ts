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

