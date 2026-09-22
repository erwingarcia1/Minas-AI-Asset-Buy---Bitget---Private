import { useEffect, useMemo, useState, type FormEvent } from "react";
import { autoBuyAPI, marketAPI, userAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert } from "@/components/Common";
import type { ApiKey, AutoBuyFrequency, AutoBuyPurchaseCreated, ChainInfo, CoinMarket } from "@/types";
import { DEFAULT_CHAIN_FOR_COIN, addressVerdictForChain } from "@/utils/chains";
import { fmtCrypto } from "@/utils/format";
import { AUTOBUY_CRYPTOS, AUTOBUY_FREQUENCIES } from "@/utils/exchangeNetworks";
import { DestinationFields, destinationBody, destinationOk, type DestinationState } from "./DestinationFields";
import { capitalize } from "@/utils/format";

const DEFAULTS = { crypto: "BTC", amount: "50", frequency: "weekly" as AutoBuyFrequency, address: "", withdraw: true };

/** Sanity only: the platform invents no minimum. The floor is Bitget's live figure for the
 *  mode (order minimum when accumulating; chain minimum + fee when withdrawing). */
export function validateAmountUsd(value: number): string | null {
  if (Number.isNaN(value)) return "Enter an amount.";
  if (value <= 0) return "Enter an amount above zero.";
  if (value > 1_000_000) return "Maximum is 1,000,000 USD.";
  return null;
}

/** The chain's smallest per-buy figure, with the plain reason it sits 1% above Bitget's own numbers. */
export const HEADROOM_NOTE = "includes 1% headroom for the price moving between creation and execution";

/** Fee / minimum line for a chain, priced when the API had an amount to price against. */
export function chainSummary(c: ChainInfo, coin: string): string {
  const fee = `fee ${fmtCrypto(c.withdraw_fee, coin)}${c.fee_usd != null ? ` (~$${c.fee_usd.toFixed(2)})` : ""}`;
  const min = `min ${fmtCrypto(c.min_withdraw_amount, coin)}${c.min_purchase_usd != null ? ` (≈ $${c.min_purchase_usd.toFixed(2)} per buy, ${HEADROOM_NOTE})` : ""}`;
  return `${fee} · ${min}`;
}

/** One form for both modes: "Withdraw immediately" on = Auto-Buy to wallet, off = accumulate on the exchange. */
export default function PurchaseForm({ onCreated }: { onCreated?: (r: AutoBuyPurchaseCreated) => void }) {
  const [accounts, setAccounts] = useState<ApiKey[]>([]);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  const [crypto, setCrypto] = useState(DEFAULTS.crypto);
  const [amount, setAmount] = useState(DEFAULTS.amount);
  const [chain, setChain] = useState<string>(DEFAULT_CHAIN_FOR_COIN[DEFAULTS.crypto]);
  const [address, setAddress] = useState(DEFAULTS.address);
  const [addressTouched, setAddressTouched] = useState(false);
  const [destination, setDestination] = useState<DestinationState>({ destinationType: "", memo: "" });
  const [accountId, setAccountId] = useState("");
  const [frequency, setFrequency] = useState<AutoBuyFrequency>(DEFAULTS.frequency);
  const [withdraw, setWithdraw] = useState(DEFAULTS.withdraw);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [market, setMarket] = useState<CoinMarket | null>(null);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [marketLoading, setMarketLoading] = useState(false);

  useEffect(() => {
    userAPI.exchangeKeys()
      .then((keys) => { setAccounts(keys); setAccountId((cur) => cur || keys[0]?.id || ""); })
      .catch((e) => setAccountsError(errorMessage(e, "Could not load exchange accounts")));
  }, []);

  // Bitget pair + chains (fee, minimum) for the chosen coin, priced against the amount (debounced).
  const amountNumber = Number(amount);
  useEffect(() => {
    let cancelled = false;
    setMarketLoading(true); setMarketError(null);
    const t = setTimeout(() => {
      marketAPI.coin(crypto, Number.isFinite(amountNumber) && amountNumber >= 1 ? amountNumber : undefined)
        .then((m) => {
          if (cancelled) return;
          setMarket(m);
          setChain((cur) => (m.chains.some((c) => c.chain === cur && c.supported) ? cur
            : m.chains.find((c) => c.is_default && c.supported)?.chain ?? m.chains.find((c) => c.supported)?.chain ?? cur));
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setMarket(null);
          // never surface raw exchange errors or cache keys: log them, show a plain message
          console.warn("market data unavailable", e);
          const status = (e as { response?: { status?: number } })?.response?.status;
          setMarketError(status === 404 ? `${crypto} cannot be bought on Bitget right now.` : "Bitget market data is temporarily unavailable. Please try again in a minute.");
        })
        .finally(() => { if (!cancelled) setMarketLoading(false); });
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [crypto, amountNumber]);

  // Any edit after a successful submit starts a fresh entry: drop the stale banner.
  const edited = <T,>(set: (v: T) => void) => (v: T) => { setSuccess(null); set(v); };
  const [setCryptoE, setAmountE, setChainE, setAddressE, setAccountIdE, setFrequencyE, setWithdrawE] =
    [edited(setCrypto), edited(setAmount), edited(setChain), edited(setAddress), edited(setAccountId), edited(setFrequency), edited(setWithdraw)];

  const account = accounts.find((a) => a.id === accountId);
  const chains = useMemo(() => (market?.chains ?? []).filter((c) => c.supported), [market]);
  const chainInfo = chains.find((c) => c.chain === chain) ?? null;
  const hasAddress = address.trim().length > 0;
  // Validation runs on blur (touched) and, once touched, on every keystroke.
  const verdict = useMemo(() => (addressTouched || hasAddress) && hasAddress ? addressVerdictForChain(address, chain) : null, [address, chain, addressTouched, hasAddress]);
  const addressMissing = withdraw && !hasAddress && addressTouched;   // required only when withdrawing
  const amountError = validateAmountUsd(amountNumber);
  const pairWarning = market && !market.online ? `${market.pair} is not trading on Bitget right now.` : null;
  const chainWarning = withdraw && chainInfo
    ? !chainInfo.withdrawable ? `Bitget has ${crypto} withdrawals on ${chain} suspended.`
      : chainInfo.meets_minimum === false ? `${amountNumber || 0} USD buys about ${fmtCrypto(chainInfo.estimated_received ?? 0, crypto)} after fees, below Bitget's minimum withdrawal on ${chain}. Use at least about $${(chainInfo.min_purchase_usd ?? 0).toFixed(2)} (${HEADROOM_NOTE}).`
      : null
    : null;
  // Accumulate never withdraws: the only floor is Bitget's minimum order for the pair (live).
  const orderMinWarning = market && !amountError && amountNumber < market.min_trade_usdt
    ? `Bitget's minimum order for ${market.pair} is ${market.min_trade_usdt} USDT.` : null;
  const chainNeeded = withdraw || hasAddress;   // accumulate without a wallet: coins stay on the exchange, no chain
  // Tag chains (live Bitget needTag): the customer must say whether the address is self-custody or an exchange.
  const needsTag = chainNeeded && hasAddress && !!chainInfo?.need_tag;
  const destinationReady = destinationOk(chain, needsTag, destination);
  const marketBlocks = withdraw && (marketError !== null || (market !== null && !chainInfo));
  // withdraw: a valid address is required; accumulate: no address, or a valid one
  const addressOk = withdraw ? verdict?.ok === true : (!hasAddress || verdict?.ok === true);
  const canSubmit = !busy && !!account && !amountError && !orderMinWarning && addressOk && destinationReady && !chainWarning && !pairWarning && !marketBlocks && !marketLoading;

  function reset() {
    setCrypto(DEFAULTS.crypto); setAmount(DEFAULTS.amount); setChain(DEFAULT_CHAIN_FOR_COIN[DEFAULTS.crypto]);
    setAddress(DEFAULTS.address); setAddressTouched(false); setFrequency(DEFAULTS.frequency); setWithdraw(DEFAULTS.withdraw);
    setDestination({ destinationType: "", memo: "" });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setAddressTouched(true);
    setError(null);
    if (!canSubmit || !account) return;
    setBusy(true);
    try {
      const created = await autoBuyAPI.create({
        crypto_symbol: crypto, amount_usd: amountNumber,
        ...(address.trim() ? { wallet_address: address.trim() } : {}),
        ...(chainNeeded ? { blockchain_network: chain } : {}),
        ...destinationBody(needsTag, destination),
        frequency, exchange_account_id: account.id,
        purchase_type: withdraw ? "autobuy" : "dca",
        withdraw_immediately: withdraw,
      });
      const when = created.next_execution ? new Date(created.next_execution).toLocaleString() : "the next scheduler run";
      const memoNote = needsTag ? (destination.destinationType === "exchange" ? ` with ${chainInfo?.tag_label ?? "memo"} ${destination.memo.trim()}` : ` (self-custody, no ${chainInfo?.tag_label ?? "memo"})`) : "";
      const mode = withdraw ? `each buy is sent to your wallet on ${chain}${memoNote}` : "accumulating on exchange";
      const banner = `Purchase created (${withdraw ? "autobuy" : "dca"}): ${crypto} for ${amountNumber.toFixed(2)} USD via ${account.exchange}, ${mode}. First execution at ${when}.`;
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
      {marketError && <Alert kind={withdraw ? "error" : "warning"}>{marketError}{withdraw ? " Withdraw-immediately purchases need live chain data." : ""}</Alert>}
      {pairWarning && <Alert kind="error">{pairWarning}</Alert>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <label className="label" htmlFor="pf-crypto">Crypto</label>
          <select id="pf-crypto" className="input" value={crypto} onChange={(e) => { setCryptoE(e.target.value); setChain(DEFAULT_CHAIN_FOR_COIN[e.target.value] ?? ""); }}>
            {AUTOBUY_CRYPTOS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          {market && <p className="mt-1 text-xs text-slate-400">{market.pair} · min order {market.min_trade_usdt} USDT{market.taker_fee_rate != null ? ` · taker fee ${(market.taker_fee_rate * 100).toFixed(2)}%` : ""}</p>}
        </div>
        <div>
          <label className="label" htmlFor="pf-amount">Amount (USD)</label>
          <input id="pf-amount" className="input" type="number" min={0.01} max={1000000} step="0.01" value={amount} onChange={(e) => setAmountE(e.target.value)} />
          {amountError && <p className="mt-1 text-xs text-red-600">{amountError}</p>}
          {!amountError && orderMinWarning && <p className="mt-1 text-xs text-red-600" data-testid="order-minimum">{orderMinWarning}</p>}
          {!amountError && !orderMinWarning && market && <p className="mt-1 text-xs text-slate-400">{withdraw ? `Bitget's floor for this mode: the ${chain} withdrawal minimum plus fee (shown under the network).` : `No minimum beyond Bitget's ${market.min_trade_usdt} USDT order minimum: coins stay on the exchange.`}</p>}
        </div>
        {chainNeeded ? <div>
          <label className="label" htmlFor="pf-network">Blockchain network</label>
          <select id="pf-network" className="input" value={chain} onChange={(e) => setChainE(e.target.value)} disabled={marketLoading || chains.length === 0}>
            {chains.length === 0 && <option value={chain}>{marketLoading ? "Loading chains…" : chain || "—"}</option>}
            {chains.map((c) => <option key={c.chain} value={c.chain} disabled={withdraw && !c.withdrawable}>{c.chain}{c.family_label ? ` · ${c.family_label}` : ""}{!c.withdrawable ? " (withdrawals suspended)" : ""}</option>)}
          </select>
          <p className="mt-1 text-xs text-slate-400" data-testid="chain-summary">
            {chainInfo ? chainSummary(chainInfo, crypto) : marketLoading ? "Fetching Bitget chain data…" : "Bitget chain data unavailable"}
          </p>
        </div> : <div>
          <span className="label">Delivery</span>
          <p className="input bg-slate-50 text-slate-600" data-testid="held-on-exchange">Held on {account ? capitalize(account.exchange) : "the exchange"}</p>
          <p className="mt-1 text-xs text-slate-400">No network needed: coins stay in your exchange account.</p>
        </div>}

        <div className="sm:col-span-2 lg:col-span-3">
          <label className="label" htmlFor="pf-address">
            Wallet address {withdraw ? <span className="text-red-600">*</span> : <span className="font-normal text-slate-400">(optional — accumulate never withdraws; add one later to switch)</span>}
          </label>
          <div className="relative">
            <input id="pf-address" className={`input pr-10 font-mono ${verdict ? (verdict.ok ? "border-green-500 focus:ring-green-500" : "border-red-500 focus:ring-red-500") : addressMissing ? "border-red-500" : ""}`}
                   placeholder={chainInfo?.family === "bitcoin" ? "bc1… / 1… / 3…" : chainInfo?.family === "solana" ? "Base58 public key" : chainInfo?.family === "tron" ? "T…" : chainInfo?.family === "xrpl" ? "r… or X…" : chainInfo?.family === "stellar" ? "G… or M…" : "0x…"}
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
          {withdraw && <p className="mt-1 text-xs text-slate-500">Bitget only sends to addresses saved in your Bitget address book; add this address there first.</p>}
          {needsTag && <DestinationFields coin={crypto} chain={chain} address={address} addressValid={verdict?.ok === true}
                                          value={destination} onChange={(v) => { setSuccess(null); setDestination(v); }} idPrefix="pf-dest" />}
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

      {chainWarning && <Alert kind="warning">⚠ {chainWarning}</Alert>}
      <button className="btn-primary" type="submit" disabled={!canSubmit}>{busy ? "Creating…" : "Create Purchase"}</button>
    </form>
  );
}
