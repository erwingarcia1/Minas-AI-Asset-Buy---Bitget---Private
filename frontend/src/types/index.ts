// Request/response shapes mirrored from the AutoBuy API OpenAPI spec.

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserProfile {
  id: string;
  email: string;
  kyc_status: KycStatus;
  role?: "user" | "admin";
  created_at: string;
}

export type KycStatus = "pending" | "verified" | "rejected";

export interface KycInitiateResponse {
  access_token: string;
  applicant_id: string;
}

export interface KycStatusResponse {
  kyc_status: KycStatus;            // platform status: pending | verified | rejected
  applicant_id?: string | null;
  review_status?: string | null;    // Sumsub: init (nothing submitted) | pending (under review) | completed
  reject_labels?: string[];
  rejection_reason?: string | null; // Sumsub moderation comment, when synced
  retryable?: boolean;              // false when the rejection is FINAL
  synced?: boolean;                 // true when this answer came from Sumsub rather than the stored status
}

export interface CreateApplicantRequest {
  email: string;
  first_name: string;
  last_name: string;
}

export interface CreateApplicantResponse {
  applicant_id: string;
  kyc_status: KycStatus;
  created: boolean;
}

export interface ApplicantStatusResponse {
  applicant_id: string;
  kyc_status: KycStatus;
  review_status: string;
  review_answer: string | null;
  reject_labels: string[];
}

export type Frequency = "daily" | "weekly" | "biweekly" | "monthly";

export interface PurchaseCreate {
  crypto: string;
  exchange?: string;
  amount_usdt: number;
  frequency: Frequency;
  withdrawal_address?: string | null;
}

export interface PurchaseUpdate {
  amount_usdt?: number;
  frequency?: Frequency;
  withdrawal_address?: string | null;
}

export interface Purchase {
  id: string;
  crypto: string;
  exchange: string;
  amount_usdt: number;
  frequency: Frequency;
  withdrawal_address: string | null;
  next_purchase_date: string;
  enabled: boolean;
  created_at: string;
}

export type TransactionStatus = "pending" | "completed" | "failed";

export interface Transaction {
  id: string;
  recurring_purchase_id: string | null;
  crypto: string;
  amount_crypto: number;
  cost_usdt: number;
  binance_order_id: string | null;
  exchange_rate: number | null;
  executed_at: string | null;
  status: TransactionStatus;
  error_message: string | null;
  created_at: string;
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
  [key: string]: number;
}

export interface QuickStats {
  total_invested_usdt: number;
  transaction_count: number;
  active_purchases: number;
  kyc_status: KycStatus;
}

export interface CryptoHolding {
  crypto: string;
  total_amount: number;
  total_cost_usdt: number;
}

export interface UpcomingPurchase {
  id: string;
  crypto: string;
  amount_usdt: number;
  frequency: Frequency;
  next_purchase_date: string;
}

export interface DashboardData {
  holdings: CryptoHolding[];
  active_purchases: UpcomingPurchase[];
  upcoming_purchases: UpcomingPurchase[];
  monthly_average_usdt: number;
}

export interface EmailSentResponse {
  sent: boolean;
  email_id: string | null;
}

export interface ApiKey {
  id: string;
  exchange: string;
  api_key_masked?: string;
  verified: boolean;
  created_at: string;
  [key: string]: unknown;
}

/** FastAPI error body: `detail` is a string or a list of validation errors. */
export interface ApiErrorBody {
  detail?: string | Array<{ loc: Array<string | number>; msg: string; type: string }> | { error?: string; details?: Record<string, unknown> };
  error?: string;
}

// ---- Auto-Buy purchases delivered to a wallet (/api/v2/purchases, /api/wallets)
export type AutoBuyFrequency = "one_time" | "daily" | "weekly" | "monthly";
export type AutoBuyStatus = "active" | "paused" | "completed" | "failed" | "cancelled";
export type PurchaseType = "autobuy" | "dca";   // autobuy: buy + withdraw to wallet; dca: buy + accumulate on exchange
export type BlockchainNetwork = "mainnet" | "testnet";

export interface AutoBuyTransaction {
  id: string;
  purchase_id: string;
  exchange_tx_id: string | null;
  tx_type?: "withdrawal" | "accumulation";
  status: "pending" | "success" | "failed";
  amount_executed: number | null;
  fee: number | null;
  error_message: string | null;
  executed_at: string | null;
  created_at: string;
}

export interface AutoBuyPurchase {
  id: string;
  crypto_symbol: string;
  amount_usd: number;
  frequency: AutoBuyFrequency;
  wallet_address: string;
  blockchain_network: BlockchainNetwork;
  purchase_type?: PurchaseType;     // absent on API builds older than migration 012
  withdraw_immediately?: boolean;   // absent on API builds older than migration 011
  exchange_account_id: string | null;
  exchange: string | null;
  status: AutoBuyStatus;
  failure_count: number;
  last_error: string | null;
  next_execution: string | null;
  created_at: string;
  updated_at: string;
  latest_transaction: AutoBuyTransaction | null;
}

export interface AutoBuyPurchaseCreate {
  crypto_symbol: string;
  amount_usd: number;
  wallet_address: string;           // required in both modes
  blockchain_network: BlockchainNetwork;
  frequency: AutoBuyFrequency;
  exchange_account_id: string;
  purchase_type: PurchaseType;      // autobuy = withdraw to wallet after each buy, dca = accumulate on exchange
  withdraw_immediately: boolean;    // same choice as a boolean, for API builds that predate purchase_type
}

export interface AutoBuyPurchaseCreated {
  purchase_id: string;
  status: AutoBuyStatus;
  next_execution: string | null;
}

/** Mode of a purchase as reported by the API, or null when the API build predates modes. */
export function purchaseMode(p: { purchase_type?: PurchaseType; withdraw_immediately?: boolean }): PurchaseType | null {
  if (p.purchase_type === "autobuy" || p.purchase_type === "dca") return p.purchase_type;
  if (typeof p.withdraw_immediately === "boolean") return p.withdraw_immediately ? "autobuy" : "dca";
  return null;
}
