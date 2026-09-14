export const fmtUsdt = (n: number | null | undefined) =>
  n == null ? "—" : `${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`;

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

export const FREQUENCIES = ["daily", "weekly", "biweekly", "monthly"] as const;
export const SUPPORTED_CRYPTOS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LTC"] as const;
