// Mirrors api/utils/exchange_networks.py.
/** @deprecated legacy mainnet/testnet table; withdrawals use exchange chain names (see utils/chains.ts). */
export const EXCHANGE_NETWORKS: Record<string, string[]> = {
  binance: ["mainnet", "testnet"],
  bitget: ["mainnet", "testnet"],
  okx: ["mainnet", "testnet"],
  bybit: ["mainnet", "testnet"],
};
export const AUTOBUY_CRYPTOS = ["BTC", "ETH", "USDT", "USDC", "SOL"] as const;
export const AUTOBUY_FREQUENCIES = [
  { value: "one_time", label: "One-time" }, { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" }, { value: "monthly", label: "Monthly" },
] as const;
export const NETWORKS = ["mainnet", "testnet"] as const;

export const canExchangeWithdrawToNetwork = (exchange: string, network: string) =>
  (EXCHANGE_NETWORKS[exchange.toLowerCase()] ?? []).includes(network.toLowerCase());
