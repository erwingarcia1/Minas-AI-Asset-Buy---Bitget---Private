// Mirrors api/utils/exchange_networks.py.
/** @deprecated legacy mainnet/testnet table; withdrawals use exchange chain names (see utils/chains.ts). */
export const EXCHANGE_NETWORKS: Record<string, string[]> = {
  binance: ["mainnet", "testnet"],
  bitget: ["mainnet", "testnet"],
  okx: ["mainnet", "testnet"],
  bybit: ["mainnet", "testnet"],
};
// Purchases are paid in USDT, and Bitget has no USDTUSDT pair, so USDT is not a purchase coin.
export const AUTOBUY_CRYPTOS = ["BTC", "ETH", "USDC", "SOL", "XRP", "XLM"] as const;
export const AUTOBUY_FREQUENCIES = [
  { value: "one_time", label: "One-time" }, { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" }, { value: "monthly", label: "Monthly" },
] as const;
export const NETWORKS = ["mainnet", "testnet"] as const;

export const canExchangeWithdrawToNetwork = (exchange: string, network: string) =>
  (EXCHANGE_NETWORKS[exchange.toLowerCase()] ?? []).includes(network.toLowerCase());
