import { FormEvent, useState } from "react";
import { purchases } from "../../services/api";
import type { Frequency, PurchaseCreate, RecurringPurchase } from "../../types";

export const SUPPORTED_CRYPTOS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LTC"];
const FREQUENCIES: Frequency[] = ["daily", "weekly", "biweekly", "monthly"];

export function validateAmount(value: number): string | null {
  if (Number.isNaN(value)) return "Enter an amount.";
  if (value < 10) return "Minimum is 10 USDT (Binance spot minimum).";
  if (value > 1_000_000) return "Maximum is 1,000,000 USDT.";
  return null;
}

interface Props {
  existing?: RecurringPurchase;
  onSaved: (p: RecurringPurchase) => void;
  onCancel: () => void;
}

export default function PurchaseForm({ existing, onSaved, onCancel }: Props) {
  const [crypto, setCrypto] = useState(existing?.crypto ?? "BTC");
  const [amount, setAmount] = useState(String(existing?.amount_usdt ?? 100));
  const [frequency, setFrequency] = useState<Frequency>(existing?.frequency ?? "weekly");
  const [address, setAddress] = useState(existing?.withdrawal_address ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const amountError = validateAmount(Number(amount));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (amountError) return setError(amountError);
    setBusy(true);
    setError(null);
    const payload: PurchaseCreate = {
      crypto, amount_usdt: Number(amount), frequency,
      ...(address ? { withdrawal_address: address } : {}),
    };
    try {
      const saved = existing
        ? await purchases.update(existing.id, payload)
        : await purchases.create(payload);
      onSaved(saved);
    } catch (err: unknown) {
      const data = (err as { response?: { data?: { detail?: unknown } } }).response?.data;
      const detail = data?.detail;
      setError(typeof detail === "string" ? detail
        : Array.isArray(detail) ? String((detail[0] as { msg?: string })?.msg ?? "Invalid input")
        : "Could not save the schedule.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4 rounded-lg bg-white p-6 shadow-sm">
      <h2 className="text-lg font-medium">{existing ? "Edit schedule" : "New schedule"}</h2>
      {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
      <label className="block text-sm">
        Cryptocurrency
        <select value={crypto} onChange={(e) => setCrypto(e.target.value)}
                className="mt-1 w-full rounded border p-2">
          {SUPPORTED_CRYPTOS.map((c) => <option key={c}>{c}</option>)}
        </select>
      </label>
      <label className="block text-sm">
        Amount (USDT)
        <input type="number" min={10} max={1000000} step="0.01" required value={amount}
               onChange={(e) => setAmount(e.target.value)}
               className="mt-1 w-full rounded border p-2"
               aria-invalid={Boolean(amountError)} />
        {amountError && <span className="text-xs text-red-600">{amountError}</span>}
      </label>
      <label className="block text-sm">
        Frequency
        <select value={frequency} onChange={(e) => setFrequency(e.target.value as Frequency)}
                className="mt-1 w-full rounded border p-2">
          {FREQUENCIES.map((f) => <option key={f}>{f}</option>)}
        </select>
      </label>
      <label className="block text-sm">
        Withdrawal address <span className="text-gray-400">(optional)</span>
        <input value={address} onChange={(e) => setAddress(e.target.value)}
               placeholder={`A ${crypto} address`}
               className="mt-1 w-full rounded border p-2" />
        <span className="text-xs text-gray-500">Validated against {crypto}'s address format.</span>
      </label>
      <div className="flex gap-2">
        <button disabled={busy || Boolean(amountError)}
                className="rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50">
          {busy ? "Saving…" : existing ? "Save changes" : "Create schedule"}
        </button>
        <button type="button" onClick={onCancel} className="rounded border px-4 py-2">
          Cancel
        </button>
      </div>
    </form>
  );
}
