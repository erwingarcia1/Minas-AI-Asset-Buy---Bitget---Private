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

/** One Auto-Buy execution attempt (purchase_transactions is the single source of truth). */
export interface Transaction {
  id: string;
  purchase_id: string;
  crypto: string;
  purchase_type: PurchaseType;
  tx_type: "buy" | "withdrawal" | "accumulation";
  amount_crypto: number;
  cost_usdt: number;
  exchange_rate: number | null;
  exchange: string | null;
  order_id: string | null;
  binance_order_id: string | null;   // = order_id (compatibility)
  fee: number | null;
  fee_currency: string | null;
  withdrawal_id: string | null;
  wallet_address: string | null;
  blockchain_network: string | null;
  withdrawal_fee: number | null;
  withdrawal_fee_currency: string | null;
  tx_hash: string | null;
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

export type VerificationStatus = "pending" | "verified" | "failed";

export interface ApiKey {
  id: string;
  exchange: string;
  api_key_masked?: string;
  verified: boolean;
  verification_status: VerificationStatus;
  verification_error?: string | null;     // readable reason when failed (never credentials)
  verified_at?: string | null;
  last_checked_at?: string | null;
  withdrawal_enabled?: boolean | null;    // null = the exchange does not report permissions
  created_at: string;
  [key: string]: unknown;
}

export interface KeyVerifyResponse {
  verified: boolean;
  result: "ok" | "invalid_key" | "ip_not_allowed" | "no_trade_permission" | "unreachable" | "error";
  detail: string;
  withdrawal_permission_warning: boolean;
  verification_status: VerificationStatus;
  verification_error?: string | null;
  withdrawal_enabled?: boolean | null;
  key?: ApiKey | null;
}

/** FastAPI error body: `detail` is a string or a list of validation errors. */
export interface ApiErrorBody {
  detail?: string | Array<{ loc: Array<string | number>; msg: string; type: string }> | { error?: string; details?: Record<string, unknown> };
  error?: string;
}

// ---- Auto-Buy purchases delivered to a wallet (/api/v2/purchases, /api/wallets)
export type AutoBuyFrequency = "one_time" | "daily" | "weekly" | "monthly";
export type AutoBuyStatus = "active" | "paused" | "completed" | "failed" | "cancelled" | "needs_review" | "withdrawal_failed" | "needs_info";
export type PurchaseType = "autobuy" | "dca";   // autobuy: buy + withdraw to wallet; dca: buy + accumulate on exchange
/** The exchange's chain name for a withdrawal (Bitget: "BTC", "ERC20", "SOL", "TRC20", ...). */
export type BlockchainNetwork = string;
export type ChainFamily = "bitcoin" | "ethereum" | "solana" | "tron";

export interface ChainInfo {
  chain: string;
  family: ChainFamily | null;
  family_label: string | null;
  supported: boolean;            // this platform can validate addresses for it
  withdrawable: boolean;
  withdraw_fee: number;
  extra_withdraw_fee_pct: number;
  min_withdraw_amount: number;
  withdraw_min_scale: number;
  withdraw_confirm: number;
  need_tag: boolean;
  browser_url: string | null;
  is_default: boolean;
  fee_usd?: number | null;
  min_purchase_usd?: number | null;
  estimated_received?: number | null;
  meets_minimum?: boolean | null;
}

export interface CoinMarket {
  coin: string;
  pair: string;
  online: boolean;
  min_trade_usdt: number;
  taker_fee_rate: number | null;
  quantity_precision: number;
  price_ask: number | null;
  chains: ChainInfo[];
  note: string;
}

export interface AutoBuyTransaction {
  id: string;
  purchase_id: string;
  exchange_tx_id: string | null;
  tx_type?: "buy" | "withdrawal" | "accumulation";
  status: "pending" | "success" | "failed" | "needs_review" | "needs_info";
  amount_executed: number | null;
  fee: number | null;
  error_message: string | null;
  executed_at: string | null;
  created_at: string;
  // buy rows
  order_id?: string | null;
  fill_price?: number | null;
  cost_usd?: number | null;
  fee_currency?: string | null;
  // withdrawal rows (parent_transaction_id -> the buy whose coins were sent)
  parent_transaction_id?: string | null;
  withdrawal_id?: string | null;
  withdrawal_chain?: string | null;
  withdrawal_fee?: number | null;
  withdrawal_fee_currency?: string | null;
  tx_hash?: string | null;
  raw_status?: string | null;
}

/** Optional beneficiary details some exchanges require before an API withdrawal. */
export interface TravelRuleInfo {
  first_name?: string | null;
  last_name?: string | null;
  identity_type?: string | null;
  company_name?: string | null;
  area_code?: string | null;
  member_code?: string | null;
}

export interface RetryWithdrawalResult {
  purchase_id: string;
  status: AutoBuyStatus;
  next_execution: string | null;
  withdrawal: AutoBuyTransaction;
}

export interface AutoBuyPurchase {
  id: string;
  crypto_symbol: string;
  amount_usd: number;
  frequency: AutoBuyFrequency;
  wallet_address: string | null;   // null on accumulate purchases without a wallet on file
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
  travel_rule_info?: TravelRuleInfo | null;
}

export interface AutoBuyPurchaseCreate {
  crypto_symbol: string;
  amount_usd: number;
  wallet_address?: string;          // required for autobuy; optional for dca (accumulate never withdraws)
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

// ---- analytics (GET /api/analytics/*) ------------------------------------------
export interface PerformanceHolding {
  crypto: string; total_amount: number; invested_usdt: number; average_buy_price: number;
  current_price?: number | null; market_value?: number | null;
  unrealized_pnl_usdt?: number | null; unrealized_pnl_pct?: number | null;
}
export interface Performance {
  price_available: boolean; holdings: PerformanceHolding[]; total_invested_usdt: number;
  total_market_value?: number | null; total_pnl_usdt?: number | null; total_pnl_pct?: number | null;
  best_performer?: string | null; worst_performer?: string | null;
}
export interface RecurringProjection {
  schedules: { crypto: string; frequency: string; amount_usdt: number; monthly_usdt: number }[];
  expected_monthly_usdt: number; projected_annual_usdt: number;
}
export interface VolatilityEntry { crypto: string; volatility_30d_pct?: number | null; level: string; recommendation: string }
export interface ComparisonEntry { crypto: string; average_buy_price: number; current_price?: number | null; buy_price_vs_market_pct?: number | null }
export interface Comparison { entries: ComparisonEntry[]; diversification_score: number }

// ---- dashboard timeline (GET /api/dashboard/analytics/timeline) ------------------
export interface TimelinePoint { date: string; transaction_count: number; total_usdt: number }
export interface Timeline { days: number; timeline: TimelinePoint[] }

// ---- reports (GET /api/reports/*) ------------------------------------------------
export interface ReportPerformance {
  has_data: boolean; price_available?: boolean; total_invested_usdt?: number; current_value_usdt?: number;
  roi_pct?: number | null; annualized_roi_pct?: number | null; purchase_count?: number; first_purchase?: string | null;
}

// ---- integrations (GET/POST /api/integrations/*) ---------------------------------
export interface IntegrationInfo { name: string; kind: string; status: string; detail: string }
export interface Webhook { id: string; url: string; enabled: boolean; last_status?: string | null; created_at: string }
export interface WebhookCreated extends Webhook { secret: string }

// ---- admin (/api/admin/*, role=admin enforced server-side) -----------------------
export interface AdminStats {
  total_users: number; verified_users: number; suspended_users: number; total_invested_usdt: number;
  total_transactions: number; failed_transactions_24h: number; active_purchases: number; signups_7d: number; volume_7d_usdt: number;
}
export interface AdminUserRow { id: string; email: string; role: string; kyc_status: string; suspended: boolean; created_at: string; invested_usdt: number }
export interface AdminTxRow { id: string; user_id: string; crypto: string; cost_usdt: number; status: string; error_message?: string | null; created_at: string }
export interface AdminAlert { name: string; severity: "ok" | "warning" | "critical"; value: string; detail: string }

/** PUT /api/v2/purchases/{id}: pause/resume, amount, frequency, or a mode switch.
 *  Switching to autobuy needs the wallet, its network and the address typed again. */
export interface AutoBuyPurchaseUpdate {
  status?: "active" | "paused";
  amount_usd?: number;
  frequency?: AutoBuyFrequency;
  purchase_type?: PurchaseType;
  wallet_address?: string;
  blockchain_network?: BlockchainNetwork;
  confirm_wallet_address?: string;
}
