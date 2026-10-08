// Request/response shapes mirrored from the AutoBuy API OpenAPI spec.

/** Login / refresh response. The refresh token never appears here: it lives in an
 *  httpOnly cookie; `csrf_token` must be sent as X-CSRF-Token on refresh and logout. */
export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  csrf_token: string;
}

export interface UserProfile {
  id: string;
  email: string;
  kyc_status: KycStatus;
  role?: "user" | "admin";
  created_at: string;
  /** Active 24h hold on new withdrawal addresses after a password reset/change, else null. */
  wallet_hold_until?: string | null;
  wallet_hold_reason?: "password_reset" | "password_change" | string | null;
  /** Private beta: may this user create or switch withdraw-immediately purchases? Server-enforced; the UI hides the controls. */
  withdrawals_allowed?: boolean;
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

export type TransactionStatus = "pending" | "completed" | "failed" | "simulated";

export interface Transaction {
  id: string;
  purchase_id: string;
  crypto: string;
  purchase_type: PurchaseType;
  tx_type: "buy" | "withdrawal" | "accumulation";
  net_amount?: number;        // gross minus the trading fee charged in the coin
  amount_crypto: number;
  cost_usdt: number;
  exchange_rate: number | null;
  price_precision?: number;
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
  /** Every row in the history table (buys, failed attempts, withdrawals). */
  total_transactions: number;
  /** These three are computed over successful buys only. */
  total_invested_usdt: number;
  average_purchase_usdt: number;
  [key: string]: number;
  needs_review_count: number;
}

export interface LowBalanceNotice {
  purchase_id: string;
  crypto_symbol: string;
  amount_usd: number;
  next_execution: string;
}

export interface QuickStats {
  total_invested_usdt: number;
  transaction_count: number;
  active_purchases: number;
  low_balance?: LowBalanceNotice[];   // active purchases with an outstanding low-balance warning
  kyc_status: KycStatus;
}

export interface CryptoHolding {
  price_precision?: number;
  crypto: string;
  total_amount: number;        // acquired, net of the trading fee charged in the coin
  on_exchange?: number;        // still on Bitget after real withdrawals (matches Bitget's balance)
  sent_to_wallet?: number;     // delivered to the customer's wallet by those withdrawals
  total_cost_usdt: number;
}

export interface UpcomingPurchase {
  id: string;
  crypto: string;
  amount_usdt: number;
  frequency: string;   // the purchase's own cadence (one_time | daily | weekly | monthly)
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
  permissions?: string[];                 // codes the exchange reported at verification
  extra_permissions?: string[];           // recognised codes AutoBuy does not need (non-blocking warning)
  created_at: string;
  [key: string]: unknown;
}

export interface KeyVerifyResponse {
  verified: boolean;
  result: "ok" | "invalid_key" | "ip_not_allowed" | "no_trade_permission" | "read_only_key" | "unified_account" | "unreachable" | "error";
  detail: string;
  withdrawal_permission_warning: boolean;
  verification_status: VerificationStatus;
  verification_error?: string | null;
  withdrawal_enabled?: boolean | null;
  extra_permissions?: string[];
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
export type ChainFamily = "bitcoin" | "ethereum" | "solana" | "tron" | "xrpl" | "stellar";
/** Tag chains only: who the address belongs to. Decides whether a memo is required. */
export type DestinationType = "self_custody" | "exchange";

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
  tag_label?: string | null;     // "destination tag" (XRP Ledger) / "memo" (Stellar) when need_tag
  browser_url: string | null;
  is_default: boolean;
  fee_usd?: number | null;
  min_purchase_usd?: number | null;
  estimated_sent?: number | null;
  estimated_received?: number | null;
  meets_minimum?: boolean | null;
}

export interface CoinMarket {
  coin: string;
  name?: string;
  category?: "main" | "stable" | "meme";
  category_label?: string;
  accumulate_only?: boolean;   // category property: held on the exchange, never withdraw mode
  risk_note?: string | null;   // neutral category note shown under the coin picker
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
  status: "pending" | "success" | "failed" | "needs_review" | "needs_info" | "simulated";
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
  withdrawal_memo?: string | null;
  withdrawal_chain?: string | null;
  withdrawal_fee?: number | null;
  withdrawal_fee_currency?: string | null;
  expected_received?: number | null;   // withdrawal rows: what the wallet should receive after the network fee
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
  blockchain_network: BlockchainNetwork | null;   // null on accumulate purchases: coins are held on the exchange
  wallet_destination_type?: DestinationType | null;   // tag chains: self_custody | exchange
  wallet_memo?: string | null;                        // destination tag / memo sent with withdrawals
  purchase_type?: PurchaseType;     // absent on API builds older than migration 012
  withdraw_immediately?: boolean;   // absent on API builds older than migration 011
  exchange_account_id: string | null;
  exchange: string | null;
  exchange_key_removed?: boolean;   // the key was deleted: the purchase cannot run again
  exchange_key_error?: string | null;   // the key's last verification problem (for example a Unified account); null while verified
  status: AutoBuyStatus;
  failure_count: number;
  last_error: string | null;
  next_execution: string | null;
  low_balance_for_next_run?: boolean;   // a low-balance warning is outstanding for next_execution
  low_balance_alerted_at?: string | null;
  created_at: string;
  updated_at: string;
  latest_transaction: AutoBuyTransaction | null;
  travel_rule_info?: TravelRuleInfo | null;
}

export interface AutoBuyPurchaseCreate {
  crypto_symbol: string;
  amount_usd: number;
  wallet_address?: string;          // required for autobuy; optional for dca (accumulate never withdraws)
  blockchain_network?: BlockchainNetwork;   // required for autobuy; omitted for accumulate without a wallet
  destination_type?: DestinationType;       // required on tag chains (XRP Ledger, Stellar)
  memo?: string;                            // required when destination_type is "exchange" on a tag chain
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
  price_precision?: number;   // decimals Bitget quotes the pair in
  current_price?: number | null; market_value?: number | null;
  unrealized_pnl_usdt?: number | null; unrealized_pnl_pct?: number | null;
}
export interface Performance {
  price_available: boolean; holdings: PerformanceHolding[]; total_invested_usdt: number;
  total_market_value?: number | null; total_pnl_usdt?: number | null; total_pnl_pct?: number | null;
}
export interface RecurringProjection {
  schedules: { crypto: string; frequency: string; amount_usdt: number; monthly_usdt: number }[];
  expected_monthly_usdt: number; projected_annual_usdt: number;
}
export interface VolatilityEntry { crypto: string; volatility_30d_pct?: number | null; level: string; summary: string }
export interface ComparisonEntry { crypto: string; average_buy_price: number; price_precision?: number; current_price?: number | null; buy_price_vs_market_pct?: number | null }
export interface InvestedShare { crypto: string; share_pct: number }   // share of COST invested, not of current value
export interface Comparison { entries: ComparisonEntry[]; share_of_invested: InvestedShare[] }

// ---- dashboard timeline (GET /api/dashboard/analytics/timeline) ------------------
export interface TimelinePoint { date: string; transaction_count: number; total_usdt: number }
export interface Timeline { days: number; timeline: TimelinePoint[] }

// ---- portfolio value over time (GET /api/dashboard/portfolio/series) -------------
export type SeriesRange = "1D" | "7D" | "30D" | "90D" | "All";
export interface SeriesPoint { t: number; value: number; invested: number }   // t: unix seconds (bucket end; last point = now)
export interface SeriesBuy { t: number; crypto: string; amount: number; cost_usd: number }
export interface PortfolioSeries {
  range: SeriesRange; granularity: string; bucket_seconds: number;
  points: SeriesPoint[]; buys: SeriesBuy[];
  current_value?: number | null; invested: number;
  change_usdt?: number | null; change_pct?: number | null;
  carried_forward: number; unpriced: string[]; note: string;
}

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
  signups_by_day?: { day: string; count: number }[];   // last 14 days (UTC), so a leaked invite code is visible
}
export interface AdminUserRow { id: string; email: string; role: string; kyc_status: string; suspended: boolean; created_at: string; invested_usdt: number;
  wallet_hold_until?: string | null; wallet_hold_reason?: string | null }
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
  destination_type?: DestinationType;
  memo?: string;
}

/** Notification settings. Security and failure emails are always sent; see always_on_emails. */
export interface NotificationPreferences {
  email_enabled: boolean;
  email_purchase_confirmations: boolean;
  email_weekly_summary: boolean;
  email_milestones: boolean;
  email_low_balance: boolean;
  always_on_emails: string[];
  sms_enabled: boolean;
  push_enabled: boolean;
  quiet_hours_start: number;
  quiet_hours_end: number;
  phone_number?: string | null;
  has_device_token: boolean;
}

export interface ParkedAttempt { id: string; client_oid: string | null; order_id: string | null; status: string; error_message: string | null; created_at: string; executed_at: string | null }
export interface AdminPurchaseRow {
  id: string; user_id: string; email: string | null; crypto_symbol: string; amount_usd: number; frequency: string; purchase_type: string;
  status: string; failure_count: number; last_error: string | null; next_execution: string | null; updated_at: string; parked_attempts: ParkedAttempt[];
}
export interface ExchangeLookup { attempt_id: string; client_oid: string | null; state: string | null; fill: Record<string, unknown> | null; verdict: string | null; error: string | null }
export interface PurchaseReview { purchase: AdminPurchaseRow; lookups: ExchangeLookup[]; actions: string[] }

/** GET /api/v2/market/destination: the server's verdict on an address + destination type + memo. */
export interface DestinationCheck {
  ok: boolean;
  error: string | null;
  message: string;
  needs_tag: boolean;
  tag_label: string | null;
  embedded_tag: string | null;
  funded: boolean | null;
  warning: string | null;
}

// ---- public facts for the landing page (GET /api/public/meta, anonymous) ---------
export interface PublicMeta {
  coins: string[];
  coin_categories?: import("@/utils/exchangeNetworks").CoinCategory[];
  accumulate_only?: string[];
  exchanges: string[];
  support_email: string;
  server_ip: string;
}
