import { useMemo, useState, type FormEvent } from "react";
import { autoBuyAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert } from "@/components/Common";
import type { AutoBuyPurchase, BlockchainNetwork } from "@/types";
import { addressVerdict } from "@/utils/addressValidation";
import { NETWORKS, canExchangeWithdrawToNetwork } from "@/utils/exchangeNetworks";

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
  const [network, setNetwork] = useState<BlockchainNetwork>(purchase.blockchain_network);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const verdict = useMemo(() => addressVerdict(address, purchase.crypto_symbol, network), [address, purchase.crypto_symbol, network]);
  const networkWarning = purchase.exchange && !canExchangeWithdrawToNetwork(purchase.exchange, network)
    ? `${purchase.exchange} cannot withdraw to ${network}.` : null;
  const confirmed = confirm.trim() === address.trim() && address.trim().length > 0;
  const canSubmit = !busy && verdict?.ok === true && confirmed && !networkWarning;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true); setError(null);
    try {
      await autoBuyAPI.update(purchase.id, {
        purchase_type: "autobuy", wallet_address: address.trim(), blockchain_network: network,
        confirm_wallet_address: confirm.trim(),
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
            {NETWORKS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="sw-address">Wallet address</label>
          <input id="sw-address" className={`input font-mono ${verdict ? (verdict.ok ? "border-green-500" : "border-red-500") : ""}`} autoComplete="off" spellCheck={false} value={address} onChange={(e) => setAddress(e.target.value)} />
          {verdict && <p className={`mt-1 text-xs ${verdict.ok ? "text-green-700" : "text-red-600"}`}>{verdict.reason}</p>}
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
