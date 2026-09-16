import { useEffect, useMemo, useState, type FormEvent } from "react";
import { autoBuyAPI, marketAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert } from "@/components/Common";
import type { AutoBuyPurchase, BlockchainNetwork, ChainInfo } from "@/types";
import { DEFAULT_CHAIN_FOR_COIN, addressVerdictForChain } from "@/utils/chains";
import { chainSummary } from "./PurchaseForm";
import { DestinationFields, destinationBody, destinationOk, type DestinationState } from "./DestinationFields";

interface Props {
  purchase: AutoBuyPurchase;
  onDone: () => void;
  onCancel: () => void;
}

/**
 * Switch an accumulate purchase to withdraw-immediately. Every buy from then on
 * is sent on-chain, so the user must enter a real address, pick its network and
 * type the address again; the API enforces the same checks.
 */
export function SwitchToWithdrawDialog({ purchase, onDone, onCancel }: Props) {
  const [address, setAddress] = useState(purchase.wallet_address ?? "");
  const [confirm, setConfirm] = useState("");
  const [network, setNetwork] = useState<BlockchainNetwork>(purchase.blockchain_network ?? DEFAULT_CHAIN_FOR_COIN[purchase.crypto_symbol] ?? "");
  const [chains, setChains] = useState<ChainInfo[]>([]);
  const [destination, setDestination] = useState<DestinationState>({
    destinationType: purchase.wallet_destination_type ?? "", memo: purchase.wallet_memo ?? "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    marketAPI.coin(purchase.crypto_symbol, purchase.amount_usd)
      .then((m) => setChains(m.chains.filter((c) => c.supported)))
      .catch((e) => setError(errorMessage(e, "Bitget chain data is unavailable")));
  }, [purchase.crypto_symbol, purchase.amount_usd]);

  const chainInfo = chains.find((c) => c.chain === network) ?? null;
  const verdict = useMemo(() => addressVerdictForChain(address, network), [address, network]);
  const networkWarning = chainInfo
    ? !chainInfo.withdrawable ? `Bitget has ${purchase.crypto_symbol} withdrawals on ${network} suspended.`
      : chainInfo.meets_minimum === false ? `${purchase.amount_usd} USD per buy is below Bitget's minimum withdrawal on ${network} (about $${(chainInfo.min_purchase_usd ?? 0).toFixed(2)} needed). Raise the amount first.`
      : null
    : null;
  const confirmed = confirm.trim() === address.trim() && address.trim().length > 0;
  const needsTag = !!chainInfo?.need_tag && address.trim().length > 0;
  const canSubmit = !busy && verdict?.ok === true && confirmed && !networkWarning && destinationOk(network, needsTag, destination);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true); setError(null);
    try {
      await autoBuyAPI.update(purchase.id, {
        purchase_type: "autobuy", wallet_address: address.trim(), blockchain_network: network,
        confirm_wallet_address: confirm.trim(), ...destinationBody(needsTag, destination),
      });
      onDone();
    } catch (err) { setError(errorMessage(err, "Could not switch the purchase")); }
    finally { setBusy(false); }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onCancel}>
      <form role="dialog" aria-modal="true" aria-labelledby="switch-title" className="card w-full max-w-md space-y-4" onClick={(e) => e.stopPropagation()} onSubmit={submit} noValidate>
        <h2 id="switch-title" className="text-lg font-semibold">Switch to withdraw immediately</h2>
        <p className="text-sm text-slate-600">
          From the next run on, every {purchase.crypto_symbol} buy will be sent on-chain to this wallet. Withdrawals cannot be reversed: enter an address you control and type it again to confirm.
        </p>
        {error && <Alert kind="error">{error}</Alert>}
        <div>
          <label className="label" htmlFor="sw-network">Blockchain network</label>
          <select id="sw-network" className="input" value={network} onChange={(e) => setNetwork(e.target.value as BlockchainNetwork)}>
            {chains.length === 0 && <option value={network}>{network}</option>}
            {chains.map((c) => <option key={c.chain} value={c.chain} disabled={!c.withdrawable}>{c.chain}{c.family_label ? ` · ${c.family_label}` : ""}</option>)}
          </select>
          {chainInfo && <p className="mt-1 text-xs text-slate-400">{chainSummary(chainInfo, purchase.crypto_symbol)}</p>}
          <p className="mt-1 text-xs text-slate-500">Bitget only sends to addresses saved in your Bitget address book; add this address there first.</p>
        </div>
        <div>
          <label className="label" htmlFor="sw-address">Wallet address</label>
          <input id="sw-address" className={`input font-mono ${verdict ? (verdict.ok ? "border-green-500" : "border-red-500") : ""}`} autoComplete="off" spellCheck={false} value={address} onChange={(e) => setAddress(e.target.value)} />
          {verdict && <p className={`mt-1 text-xs ${verdict.ok ? "text-green-700" : "text-red-600"}`}>{verdict.reason}</p>}
          {needsTag && <DestinationFields coin={purchase.crypto_symbol} chain={network} address={address} addressValid={verdict?.ok === true}
                                          value={destination} onChange={setDestination} idPrefix="sw-dest" />}
        </div>
        <div>
          <label className="label" htmlFor="sw-confirm">Type the address again to confirm</label>
          <input id="sw-confirm" className="input font-mono" autoComplete="off" spellCheck={false} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          {confirm && !confirmed && <p className="mt-1 text-xs text-red-600">The addresses do not match.</p>}
        </div>
        {networkWarning && <Alert kind="warning">{networkWarning}</Alert>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" className="btn-danger" disabled={!canSubmit}>{busy ? "Switching…" : "Switch to withdraw"}</button>
        </div>
      </form>
    </div>
  );
}
