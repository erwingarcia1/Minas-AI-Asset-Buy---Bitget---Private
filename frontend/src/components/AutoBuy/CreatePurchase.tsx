import { FormEvent, useEffect, useMemo, useState } from "react";
import { apiErrorMessage, autoBuy, exchange as exchangeApi } from "../../services/api";
import type { ApiKey, AutoBuyFrequency, AutoBuyPurchaseCreated, BlockchainNetwork } from "../../types";
import { addressVerdict } from "../../utils/addressValidation";
import { AUTOBUY_CRYPTOS, AUTOBUY_FREQUENCIES, EXCHANGE_NETWORKS, NETWORKS, canExchangeWithdrawToNetwork } from "../../utils/exchangeNetworks";

export function validateAmountUsd(value: number): string | null {
  if (Number.isNaN(value)) return "Enter an amount.";
  if (value < 10) return "Minimum is 10 USD.";
  if (value > 1_000_000) return "Maximum is 1,000,000 USD.";
  return null;
}

interface Props { onCreated: (p: AutoBuyPurchaseCreated) => void }

export default function CreatePurchase({ onCreated }: Props) {
  const [accounts, setAccounts] = useState<ApiKey[]>([]);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  const [crypto, setCrypto] = useState<string>("BTC");
  const [amount, setAmount] = useState("50");
  const [frequency, setFrequency] = useState<AutoBuyFrequency>("weekly");
  const [network, setNetwork] = useState<BlockchainNetwork>("mainnet");
  const [address, setAddress] = useState("");
  const [accountId, setAccountId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    exchangeApi.listKeys()
      .then((keys) => { setAccounts(keys); if (keys[0] && !accountId) setAccountId(keys[0].id); })
      .catch(() => setAccountsError("Could not load your exchange accounts."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const account = accounts.find((a) => a.id === accountId);
  const verdict = useMemo(() => addressVerdict(address, crypto, network), [address, crypto, network]);
  const amountError = validateAmountUsd(Number(amount));
  const networkWarning = account && !canExchangeWithdrawToNetwork(account.exchange, network)
    ? `${account.exchange} cannot withdraw to ${network} (supports: ${(EXCHANGE_NETWORKS[account.exchange] ?? []).join(", ") || "none"}).`
    : null;
  const canSubmit = !busy && !!account && !amountError && !!verdict?.ok && !networkWarning;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit || !account) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await autoBuy.create({
        crypto_symbol: crypto, amount_usd: Number(amount), wallet_address: address.trim(),
        blockchain_network: network, frequency, exchange_account_id: account.id,
      }));
      setAddress("");
    } catch (err) {
      setError(apiErrorMessage(err, "Could not create the purchase."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4 rounded-lg bg-white p-6 shadow-sm" aria-label="Create auto-buy purchase">
      <h2 className="text-lg font-medium">New auto-buy</h2>
      {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
      {accountsError && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{accountsError}</p>}

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block text-sm">Crypto
          <select className="mt-1 w-full rounded border px-3 py-2" value={crypto} onChange={(e) => setCrypto(e.target.value)}>
            {AUTOBUY_CRYPTOS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="block text-sm">Amount (USD)
          <input type="number" min={10} max={1000000} step="0.01" className="mt-1 w-full rounded border px-3 py-2"
                 value={amount} onChange={(e) => setAmount(e.target.value)} />
          {amountError && amount !== "" && <span className="text-xs text-red-600">{amountError}</span>}
        </label>
        <label className="block text-sm">Frequency
          <select className="mt-1 w-full rounded border px-3 py-2" value={frequency} onChange={(e) => setFrequency(e.target.value as AutoBuyFrequency)}>
            {AUTOBUY_FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </label>
        <label className="block text-sm">Blockchain network
          <select className="mt-1 w-full rounded border px-3 py-2" value={network} onChange={(e) => setNetwork(e.target.value as BlockchainNetwork)}>
            {NETWORKS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="block text-sm sm:col-span-2">Wallet address
          <span className="relative mt-1 block">
            <input className="w-full rounded border px-3 py-2 pr-9 font-mono text-sm" placeholder={crypto === "BTC" ? "bc1… / 1… / 3…" : crypto === "SOL" ? "Base58 public key" : "0x…"}
                   value={address} onChange={(e) => setAddress(e.target.value)} aria-describedby="address-verdict" />
            {verdict && (
              <span data-testid="address-indicator" aria-label={verdict.ok ? "valid address" : "invalid address"}
                    className={`absolute right-3 top-2.5 text-lg ${verdict.ok ? "text-green-600" : "text-red-600"}`}>
                {verdict.ok ? "✓" : "✗"}
              </span>
            )}
          </span>
          {verdict && <span id="address-verdict" className={`text-xs ${verdict.ok ? "text-green-700" : "text-red-600"}`}>{verdict.reason}</span>}
        </label>
        <label className="block text-sm sm:col-span-2">Exchange account
          <select className="mt-1 w-full rounded border px-3 py-2" value={accountId} onChange={(e) => setAccountId(e.target.value)} disabled={accounts.length === 0}>
            {accounts.length === 0 && <option value="">No exchange accounts — add an API key in Settings</option>}
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.exchange} · {a.api_key_masked}{a.verified ? "" : " (unverified)"}</option>)}
          </select>
        </label>
      </div>
      {networkWarning && <p role="status" className="rounded bg-amber-50 p-2 text-sm text-amber-800">⚠ {networkWarning}</p>}
      <button type="submit" disabled={!canSubmit} className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
        {busy ? "Creating…" : "Create auto-buy"}
      </button>
    </form>
  );
}
