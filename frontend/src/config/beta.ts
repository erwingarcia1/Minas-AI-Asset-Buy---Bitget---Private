/** Private-beta switches. Build-time (Vite): VITE_BETA_TICKER=0 removes the
 *  ticker from every page at launch; anything else (or unset) keeps it. */
export const BETA_TICKER_ENABLED = (import.meta.env.VITE_BETA_TICKER ?? "1") !== "0";

export const BETA_TICKER_TEXT =
  "Private beta · Test mode · By invitation only · Live orders use your real Bitget funds · Not financial advice";

/** Shown when /api/public/meta cannot be read (the one place this literal lives). */
export const SUPPORT_EMAIL_FALLBACK = "support@minastech.ca";
