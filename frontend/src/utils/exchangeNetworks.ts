// Purchase coins and cadences the platform offers (the API enforces the same lists and serves
// the categories on /api/public/meta and /api/v2/market/coins/{coin}; this is the fallback copy).
// Purchases are paid in USDT, and Bitget has no USDTUSDT pair, so USDT is not a purchase coin.
export type CoinCategoryKey = "main" | "stable" | "meme";

export interface CoinCategory {
  category: CoinCategoryKey;
  label: string;             // descriptive only; never ranks or recommends
  accumulate_only: boolean;  // bought and held on the exchange; withdraw mode never offered
  risk_note: string | null;
  coins: { symbol: string; name: string }[];
}

export const COIN_CATEGORIES: CoinCategory[] = [
  { category: "main", label: "Main coins", accumulate_only: false, risk_note: null,
    coins: [{ symbol: "BTC", name: "Bitcoin" }, { symbol: "ETH", name: "Ethereum" }, { symbol: "SOL", name: "Solana" }, { symbol: "XRP", name: "XRP" }, { symbol: "XLM", name: "Stellar" }] },
  { category: "stable", label: "Stablecoins", accumulate_only: false, risk_note: null,
    coins: [{ symbol: "USDC", name: "USD Coin" }] },
  { category: "meme", label: "Meme coins", accumulate_only: true,
    risk_note: "Meme coins can move very sharply in price. Only buy what you can afford to lose.",
    coins: [{ symbol: "DOGE", name: "Dogecoin" }, { symbol: "SHIB", name: "Shiba Inu" }, { symbol: "PEPE", name: "Pepe" }, { symbol: "BONK", name: "Bonk" }] },
];

export const AUTOBUY_CRYPTOS = COIN_CATEGORIES.flatMap((g) => g.coins.map((c) => c.symbol)) as readonly string[];
export const ACCUMULATE_ONLY = new Set(COIN_CATEGORIES.filter((g) => g.accumulate_only).flatMap((g) => g.coins.map((c) => c.symbol)));

export function categoryOf(symbol: string, groups: CoinCategory[] = COIN_CATEGORIES): CoinCategory | null {
  return groups.find((g) => g.coins.some((c) => c.symbol === symbol)) ?? null;
}
export function coinName(symbol: string, groups: CoinCategory[] = COIN_CATEGORIES): string {
  return groups.flatMap((g) => g.coins).find((c) => c.symbol === symbol)?.name ?? symbol;
}
export function isAccumulateOnly(symbol: string | null | undefined, groups: CoinCategory[] = COIN_CATEGORIES): boolean {
  return !!symbol && (categoryOf(symbol, groups)?.accumulate_only ?? false);
}

export const AUTOBUY_FREQUENCIES = [
  { value: "one_time", label: "One-time" }, { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" }, { value: "monthly", label: "Monthly" },
] as const;
