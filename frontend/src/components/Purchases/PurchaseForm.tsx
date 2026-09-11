import { useEffect, useMemo, useState, type FormEvent } from "react";
import { autoBuyAPI, userAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert } from "@/components/Common";
import type { ApiKey, AutoBuyFrequency, AutoBuyPurchaseCreated, BlockchainNetwork } from "@/types";
import { addressVerdict } from "@/utils/addressValidation";
import { AUTOBUY_CRYPTOS, AUTOBUY_FREQUENCIES, EXCHANGE_NETWORKS, NETWORKS, canExchangeWithdrawToNetwork } from "@/utils/exchangeNetworks";

const DEFAULTS = { crypto: "BTC", amount: "50", network: "mainnet" as BlockchainNetwork, frequency: "weekly" as AutoBuyFrequency, address: "", withdraw: true };

export function validateAmountUsd(value: number): string | null {
  if (Number.isNaN(value)) return "Enter an amount.";
  if (value < 10) return "Minimum is 10 USD.";
  if (value > 1_000_000) return "Maximum is 1,000,000 USD.";
  return null;
}

/** One form for both modes: "Withdraw immediately" on = Auto-Buy to wallet, off = accumulate on the exchange. */
export default function PurchaseForm({ onCreated }: { onCreated?: (r: AutoBuyPurchaseCreated) => void }) {
  const [accounts, setAccounts] = useState<ApiKey[]>([]);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  const [crypto, setCrypto] = useState(DEFAULTS.crypto);
  const [amount, setAmount] = useState(DEFAULTS.amount);
  const [network, setNetwork] = useState<BlockchainNetwork>(DEFAULTS.network);
  const [address, setAddress] = useState(DEFAULTS.address);
  const [addressTouched, setAddressTouched] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [frequency, setFrequency] = useState<AutoBuyFrequency>(DEFAULTS.frequency);
  const [withdraw, setWithdraw] = useState(DEFAULTS.withdraw);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    userAPI.exchangeKeys()
      .then((keys) => { setAccounts(keys); setAccountId((cur) => cur || keys[0]?.id || ""); })
      .catch((e) => setAccountsError(errorMessage(e, "Could not load exchange accounts")));
  }, []);
  // Any edit after a successful submit starts a fresh entry: drop the stale banner.
  const edited = <T,>(set: (v: T) => void) => (v: T) => { setSuccess(null); set(v); };
  const [setCryptoE, setAmountE, setNetworkE, setAddressE, setAccountIdE, setFrequencyE, setWithdrawE] =
    [edited(setCrypto), edited(setAmount), edited(setNetwork), edited(setAddress), edited(setAccountId), edited(setFrequency), edited(setWithdraw)];

  const account = accounts.find((a) => a.id === accountId);
  const hasAddress = address.trim().length > 0;
  // Validation runs on blur (touched) and, once touched, on every keystroke.
  const verdict = useMemo(() => (addressTouched || hasAddress) && hasAddress ? addressVerdict(address, crypto, network) : null, [address, crypto, network, addressTouched, hasAddress]);
  const addressMissing = withdraw && !hasAddress && addressTouched;   // required only when withdrawing
  const amountError = validateAmountUsd(Number(amount));
  const networkWarning = withdraw && account && !canExchangeWithdrawToNetwork(account.exchange, network)
    ? `${account.exchange} does not support withdrawals to ${network}. Supported: ${(EXCHANGE_NETWORKS[account.exchange] ?? []).join(", ") || "none"}.`
    : null;
  // withdraw: a valid address is required; accumulate: no address, or a valid one
  const addressOk = withdraw ? verdict?.ok === true : (!hasAddress || verdict?.ok === true);
  const canSubmit = !busy && !!account && !amountError && addressOk && !networkWarning;

  function reset() {
    setCrypto(DEFAULTS.crypto); setAmount(DEFAULTS.amount); setNetwork(DEFAULTS.network);
    setAddress(DEFAULTS.address); setAddressTouched(false); setFrequency(DEFAULTS.frequency); setWithdraw(DEFAULTS.withdraw);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setAddressTouched(true);
    setError(null);
    if (!canSubmit || !account) return;
    setBusy(true);
    try {
      const created = await autoBuyAPI.create({
        crypto_symbol: crypto, amount_usd: Number(amount),
        ...(address.trim() ? { wallet_address: address.trim() } : {}),
        blockchain_network: network, frequency, exchange_account_id: account.id,
        purchase_type: withdraw ? "autobuy" : "dca",
        withdraw_immediately: withdraw,
      });
      const when = created.next_execution ? new Date(created.next_execution).toLocaleString() : "the next scheduler run";
      const mode = withdraw ? `each buy is sent to your wallet on ${network}` : "accumulating on exchange";
      const banner = `Purchase created (${withdraw ? "autobuy" : "dca"}): ${crypto} for ${Number(amount).toFixed(2)} USD via ${account.exchange}, ${mode}. First execution at ${when}.`;
      reset();
      setSuccess(banner);
      onCreated?.(created);
    } catch (err) {
      setError(errorMessage(err, "Could not create the purchase."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4" aria-label="Create purchase" noValidate>
      <div>
        <h2 className="text-lg font-semibold">Create purchase</h2>
        <p className="mt-1 text-sm text-slate-500">Buys on your exchange account on a schedule. Choose whether each buy is sent to your wallet right away or held on the exchange.</p>
      </div>
      {success && <Alert kind="success">{success}</Alert>}
      {error && <Alert kind="error">{error}</Alert>}
      {accountsError && <Alert kind="error">{accountsError}</Alert>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <label className="label" htmlFor="pf-crypto">Crypto</label>
          <select id="pf-crypto" className="input" value={crypto} onChange={(e) => setCryptoE(e.target.value)}>
            {AUTOBUY_CRYPTOS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="pf-amount">Amount (USD)</label>
          <input id="pf-amount" className="input" type="number" min={10} max={1000000} step="0.01" value={amount} onChange={(e) => setAmountE(e.target.value)} />
          {amountError && <p className="mt-1 text-xs text-red-600">{amountError}</p>}
        </div>
        <div>
          <label className="label" htmlFor="pf-network">Blockchain network</label>
          <select id="pf-network" className="input" value={network} onChange={(e) => setNetworkE(e.target.value as BlockchainNetwork)}>
            {NETWORKS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <p className="mt-1 text-xs text-slate-400">{crypto === "BTC" ? "Bitcoin addresses encode their network." : `${crypto} uses the same address on both networks.`}</p>
        </div>

        <div className="sm:col-span-2 lg:col-span-3">
          <label className="label" htmlFor="pf-address">
            Wallet address {withdraw ? <span className="text-red-600">*</span> : <span className="font-normal text-slate-400">(optional — accumulate never withdraws; add one later to switch)</span>}
          </label>
          <div className="relative">
            <input id="pf-address" className={`input pr-10 font-mono ${verdict ? (verdict.ok ? "border-green-500 focus:ring-green-500" : "border-red-500 focus:ring-red-500") : addressMissing ? "border-red-500" : ""}`}
                   placeholder={crypto === "BTC" ? "bc1… / 1… / 3…" : crypto === "SOL" ? "Base58 public key" : "0x…"}
                   value={address} autoComplete="off" spellCheck={false}
                   onChange={(e) => setAddressE(e.target.value)} onBlur={() => setAddressTouched(true)}
                   aria-invalid={verdict ? !verdict.ok : addressMissing || undefined} aria-describedby="pf-address-status" />
            {verdict && (
              <span data-testid="address-indicator" aria-label={verdict.ok ? "valid address" : "invalid address"}
                    className={`pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-lg font-semibold ${verdict.ok ? "text-green-600" : "text-red-600"}`}>
                {verdict.ok ? "✓" : "✗"}
              </span>
            )}
          </div>
          {verdict && <p id="pf-address-status" className={`mt-1 text-xs ${verdict.ok ? "text-green-700" : "text-red-600"}`}>{verdict.reason}</p>}
          {!verdict && addressMissing && <p id="pf-address-status" className="mt-1 text-xs text-red-600">A wallet address is required.</p>}
        </div>

        <div className="sm:col-span-2">
          <label className="label" htmlFor="pf-account">Exchange account</label>
          <select id="pf-account" className="input" value={accountId} onChange={(e) => setAccountIdE(e.target.value)} disabled={accounts.length === 0}>
            {accounts.length === 0 && <option value="">No exchange accounts. Add an API key in Settings.</option>}
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.exchange} · {String(a.api_key_masked ?? "")}{a.verified ? "" : " (unverified)"}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="pf-frequency">Frequency</label>
          <select id="pf-frequency" className="input" value={frequency} onChange={(e) => setFrequencyE(e.target.value as AutoBuyFrequency)}>
            {AUTOBUY_FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
      </div>

      <label className="flex cursor-pointer items-start gap-3 rounded-md border border-slate-200 bg-slate-50 p-3">
        <input id="pf-withdraw" type="checkbox" className="mt-1 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
               checked={withdraw} onChange={(e) => setWithdrawE(e.target.checked)} />
        <span className="text-sm">
          <span className="font-medium text-slate-900">Withdraw immediately</span>
          <span className="block text-slate-600">
            {withdraw
              ? "Auto-Buy: each purchase is bought on the exchange and sent to your wallet right away."
              : "Accumulate on exchange (DCA): purchases are bought and held on the exchange. No wallet is needed; you can switch to withdrawing later."}
          </span>
        </span>
      </label>

      {networkWarning && <Alert kind="warning">⚠ {networkWarning}</Alert>}
      <button className="btn-primary" type="submit" disabled={!canSubmit}>{busy ? "Creating…" : "Create Purchase"}</button>
    </form>
  );
}
