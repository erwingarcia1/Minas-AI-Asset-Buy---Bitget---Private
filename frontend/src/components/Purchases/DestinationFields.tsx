import { useEffect, useState } from "react";
import { marketAPI } from "@/api/endpoints";
import { Alert } from "@/components/Common";
import type { DestinationType } from "@/types";
import { parseTaggedAddress, tagLabelForChain, tagVerdictForChain } from "@/utils/chains";

/** Wallet apps named per chain so the customer can tell self-custody from an exchange. */
const WALLET_EXAMPLES: Record<string, string> = {
  XRP: "Ledger, Xaman, Trust Wallet, a hardware or software wallet",
  XLM: "Lobstr, Freighter, Ledger, a hardware or software wallet",
};

export interface DestinationState {
  destinationType: DestinationType | "";
  memo: string;
}

interface Props {
  coin: string;
  chain: string;
  address: string;          // as typed; an X-address / M-address carries its own tag
  addressValid: boolean;
  value: DestinationState;
  onChange: (v: DestinationState) => void;
  idPrefix?: string;
}

/**
 * The required question under the address on a tag chain (XRP Ledger, Stellar):
 * is the address a wallet the customer controls (no tag) or an exchange /
 * custody account (tag required)? The API refuses the purchase until it is
 * answered; this component mirrors that rule so the form never submits a
 * destination the server would reject. Wording approved 2026-09-16.
 */
export function DestinationFields({ coin, chain, address, addressValid, value, onChange, idPrefix = "dest" }: Props) {
  const label = tagLabelForChain(chain) ?? "memo";
  const parsed = addressValid ? parseTaggedAddress(address, chain) : null;
  const embedded = parsed?.tag ?? null;
  const [funded, setFunded] = useState<{ address: string; warning: string | null } | null>(null);

  // An X-address / muxed address decides for the customer: exchange, tag read-only.
  useEffect(() => {
    if (embedded !== null && (value.destinationType !== "exchange" || value.memo !== embedded)) onChange({ destinationType: "exchange", memo: embedded });
  }, [embedded]); // eslint-disable-line react-hooks/exhaustive-deps

  // Typo detector: ask the public ledger (through the API) whether the account exists. Warns, never blocks.
  useEffect(() => {
    if (!addressValid) { setFunded(null); return; }
    const a = address.trim();
    let cancelled = false;
    marketAPI.destination({ chain, address: a })
      .then((r) => { if (!cancelled) setFunded({ address: a, warning: r.warning }); })
      .catch(() => { if (!cancelled) setFunded(null); });
    return () => { cancelled = true; };
  }, [address, chain, addressValid]);

  const tagVerdict = value.destinationType === "exchange" && value.memo.trim() ? tagVerdictForChain(value.memo, chain) : null;
  const examples = WALLET_EXAMPLES[coin] ?? "a hardware or software wallet";
  const prefix = idPrefix;

  return (
    <fieldset className="mt-3 space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3" data-testid="destination-fields">
      <legend className="px-1 text-sm font-semibold">Where is this {coin} address?</legend>
      <label className="flex cursor-pointer items-start gap-2 text-sm">
        <input type="radio" name={`${prefix}-type`} className="mt-1" checked={value.destinationType === "self_custody"} disabled={embedded !== null}
               onChange={() => onChange({ destinationType: "self_custody", memo: "" })} />
        <span><span className="font-medium">A wallet I control</span> ({examples}). No {label} is needed. If you are not sure, check in your wallet app: a self-custody wallet never asks you to enter a {label === "memo" ? "memo" : "tag"} to receive.</span>
      </label>
      <label className="flex cursor-pointer items-start gap-2 text-sm">
        <input type="radio" name={`${prefix}-type`} className="mt-1" checked={value.destinationType === "exchange"}
               onChange={() => onChange({ destinationType: "exchange", memo: embedded ?? value.memo })} />
        <span><span className="font-medium">An exchange or custody account</span> (Binance, Coinbase, Kraken, a broker). These give you a <span className="font-medium">{label}</span> alongside the address. Enter it below exactly as shown.</span>
      </label>
      {value.destinationType === "exchange" && (
        <div>
          <label className="label" htmlFor={`${prefix}-memo`}>{label[0].toUpperCase() + label.slice(1)}</label>
          <input id={`${prefix}-memo`} className={`input font-mono ${tagVerdict ? (tagVerdict.ok ? "border-green-500" : "border-red-500") : ""}`}
                 value={value.memo} readOnly={embedded !== null} autoComplete="off" spellCheck={false}
                 onChange={(e) => onChange({ destinationType: "exchange", memo: e.target.value })}
                 aria-invalid={tagVerdict ? !tagVerdict.ok : undefined} />
          {embedded !== null && <p className="mt-1 text-xs text-slate-500">This address already contains {label} {embedded}; leave the {label} field empty or paste a plain address.</p>}
          {tagVerdict && !tagVerdict.ok && <p className="mt-1 text-xs text-red-600">{tagVerdict.reason}</p>}
          {!value.memo.trim() && embedded === null && <p className="mt-1 text-xs text-red-600">The {label} is required for an exchange or custody address.</p>}
        </div>
      )}
      {value.destinationType === "" && <p className="text-xs text-red-600" data-testid="destination-required">Choose one before continuing.</p>}
      <Alert kind="warning">
        Sending to an exchange <span className="font-semibold">without its {label}</span> puts your {coin} in the exchange's shared account with nothing linking it to you. Recovery is slow, often paid, and sometimes impossible. We cannot reverse it.
      </Alert>
      {funded?.warning && funded.address === address.trim() && <Alert kind="warning"><span data-testid="unfunded-warning">{funded.warning}</span></Alert>}
    </fieldset>
  );
}

/** Mirrors the API rule: on a tag chain the destination must be answered; exchange needs a valid memo. */
export function destinationOk(chain: string, needsTag: boolean, value: DestinationState): boolean {
  if (!needsTag) return true;
  if (value.destinationType === "self_custody") return true;
  if (value.destinationType === "exchange") return !!value.memo.trim() && tagVerdictForChain(value.memo, chain).ok;
  return false;
}

/** Request-body fields for a destination on a tag chain; nothing on other chains. */
export function destinationBody(needsTag: boolean, value: DestinationState): { destination_type?: DestinationType; memo?: string } {
  if (!needsTag || !value.destinationType) return {};
  return value.destinationType === "exchange" ? { destination_type: "exchange", memo: value.memo.trim() } : { destination_type: "self_custody" };
}
