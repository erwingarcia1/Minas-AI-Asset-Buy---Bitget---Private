// Types mirroring the API schemas (see /openapi.json, version 0.2.0).

export interface User {
  id: string;
  email: string;
  role?: "user" | "admin";
  kyc_status: "pending" | "verified" | "rejected";
  kyc_sumsub_id?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface ApiKey {
  id: string;
  exchange: string;
  api_key_masked: string;
  verified: boolean;
  created_at: string;
  updated_at: string;
}

export type Frequency = "daily" | "weekly" | "biweekly" | "monthly";

export interface RecurringPurchase {
  id: string;
  crypto: string;
  amount_usdt: number;
  frequency: Frequency;
  next_purchase_date: string;
  withdrawal_address?: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface PurchaseCreate {
  crypto: string;
  amount_usdt: number;
  frequency: Frequency;
  withdrawal_address?: string;
}

export interface Transaction {
  id: string;
  recurring_purchase_id?: string | null;
  crypto: string;
  amount_crypto: number;
  cost_usdt: number;
  binance_order_id?: string | null;
  exchange_rate?: number | null;
  executed_at?: string | null;
  status: "pending" | "completed" | "failed";
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TransactionList {
  items: Transaction[];
  total: number;
  limit: number;
  offset: number;
}

export interface TransactionStats {
  total_transactions: number;
  total_invested_usdt: number;
  average_purchase_usdt: number;
  completed_count: number;
  failed_count: number;
  pending_count: number;
}

export interface CryptoHolding {
  crypto: string;
  total_amount: number;
  total_cost_usdt: number;
}

export interface Dashboard {
  holdings: CryptoHolding[];
  active_purchases: RecurringPurchase[];
  upcoming_purchases: RecurringPurchase[];
  monthly_average_usdt: number;
}

export interface QuickStats {
  total_invested_usdt: number;
  transaction_count: number;
  active_purchases: number;
  kyc_status: string;
}

export interface TimelinePoint {
  date: string;
  transaction_count: number;
  total_usdt: number;
}

export interface Timeline {
  days: number;
  timeline: TimelinePoint[];
}

// ---- Auto-Buy purchases (delivered to a wallet) — /api/v2/purchases, /api/wallets
export type AutoBuyFrequency = "one_time" | "daily" | "weekly" | "monthly";
export type AutoBuyStatus = "active" | "paused" | "completed" | "failed" | "cancelled";
export type BlockchainNetwork = "mainnet" | "testnet";

export interface AutoBuyTransaction {
  id: string;
  purchase_id: string;
  exchange_tx_id?: string | null;
  status: "pending" | "success" | "failed";
  amount_executed?: number | null;
  fee?: number | null;
  error_message?: string | null;
  executed_at?: string | null;
  created_at: string;
}

export interface AutoBuyPurchase {
  id: string;
  crypto_symbol: string;
  amount_usd: number;
  frequency: AutoBuyFrequency;
  wallet_address: string;
  blockchain_network: BlockchainNetwork;
  exchange_account_id?: string | null;
  exchange?: string | null;
  status: AutoBuyStatus;
  failure_count: number;
  last_error?: string | null;
  next_execution?: string | null;
  created_at: string;
  updated_at: string;
  latest_transaction?: AutoBuyTransaction | null;
  transactions?: AutoBuyTransaction[];
}

export interface AutoBuyPurchaseCreate {
  crypto_symbol: string;
  amount_usd: number;
  wallet_address: string;
  blockchain_network: BlockchainNetwork;
  frequency: AutoBuyFrequency;
  exchange_account_id: string;
}

export interface AutoBuyPurchaseCreated {
  purchase_id: string;
  status: AutoBuyStatus;
  next_execution?: string | null;
}

export interface Wallet {
  id: string;
  address: string;
  blockchain_network: BlockchainNetwork;
  chain?: "bitcoin" | "ethereum" | "solana" | null;
  label?: string | null;
  verified_at?: string | null;
  created_at: string;
}
