import { useState, type FormEvent } from "react";
import { Alert } from "@/components/Common";
import type { MfaFactor } from "@/types";
import { RECOVERY_CODE_MAX_LENGTH, RECOVERY_CODE_PLACEHOLDER } from "@/utils/validation";

interface Props {
  onSubmit: (factor: MfaFactor) => Promise<void>;
  onBack: () => void;
}

/** Second step of sign-in: a six-digit authenticator code, or one of the recovery codes. */
export function MfaCodeForm({ onSubmit, onBack }: Props) {
  const [useRecovery, setUseRecovery] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handle(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const typed = value.replace(/\s+/g, "");
    if (!useRecovery && !/^\d{6}$/.test(typed)) return setError("Enter the six-digit code from your authenticator app");
    if (useRecovery && typed.length < 10) return setError("Enter one of your recovery codes");
    setBusy(true);
    try {
      await onSubmit(useRecovery ? { recovery_code: typed } : { code: typed });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handle} className="space-y-4" noValidate data-testid="mfa-form">
      <Alert kind="error">{error}</Alert>
      <div>
        <label className="label" htmlFor="mfa_code">{useRecovery ? "Recovery code" : "Authenticator code"}</label>
        <input id="mfa_code" name="mfa_code" className="input font-mono tracking-widest" type="text" autoFocus
               inputMode={useRecovery ? "text" : "numeric"} autoComplete="one-time-code" autoCapitalize="characters" spellCheck={false}
               maxLength={useRecovery ? RECOVERY_CODE_MAX_LENGTH : 7} value={value} onChange={(e) => setValue(e.target.value)}
               placeholder={useRecovery ? RECOVERY_CODE_PLACEHOLDER : "123456"} />
        <p className="mt-1 text-xs text-slate-500">
          {useRecovery ? "Each recovery code works once. You will be emailed when one is used."
                       : "The code changes every 30 seconds. This sign-in expires after five minutes."}
        </p>
      </div>
      <button className="btn-primary w-full" type="submit" disabled={busy}>{busy ? "Please wait…" : "Continue"}</button>
      <div className="flex items-center justify-between text-sm">
        <button type="button" className="text-brand-600 hover:underline" onClick={() => { setUseRecovery((v) => !v); setValue(""); setError(null); }}>
          {useRecovery ? "Use my authenticator app instead" : "Use a recovery code instead"}
        </button>
        <button type="button" className="text-slate-500 hover:underline" onClick={onBack}>Back to sign in</button>
      </div>
    </form>
  );
}
