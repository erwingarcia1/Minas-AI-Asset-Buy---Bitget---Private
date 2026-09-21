// Purchase coins and cadences the platform offers (the API enforces the same lists).
// Purchases are paid in USDT, and Bitget has no USDTUSDT pair, so USDT is not a purchase coin.
export const AUTOBUY_CRYPTOS = ["BTC", "ETH", "USDC", "SOL", "XRP", "XLM"] as const;
export const AUTOBUY_FREQUENCIES = [
  { value: "one_time", label: "One-time" }, { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" }, { value: "monthly", label: "Monthly" },
] as const;
