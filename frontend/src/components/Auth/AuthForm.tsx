import { useState, type FormEvent } from "react";
import { Alert } from "@/components/Common";
import { isEmail, passwordProblem } from "@/utils/validation";

interface Props {
  submitLabel: string;
  requireStrongPassword?: boolean;
  /** Private beta: ask for the invite code and pass it to onSubmit. */
  requireInviteCode?: boolean;
  onSubmit: (email: string, password: string, inviteCode?: string) => Promise<void>;
}

export function AuthForm({ submitLabel, requireStrongPassword = false, requireInviteCode = false, onSubmit }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handle(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!isEmail(email)) return setError("Enter a valid email address");
    if (requireStrongPassword) {
      const p = passwordProblem(password);
      if (p) return setError(p);
      if (password !== confirm) return setError("Passwords do not match");
    } else if (!password) return setError("Enter your password");
    if (requireInviteCode && !inviteCode.trim()) return setError("Enter your invite code");
    setBusy(true);
    try {
      await onSubmit(email.trim().toLowerCase(), password, requireInviteCode ? inviteCode.trim() : undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handle} className="space-y-4" noValidate>
      <Alert kind="error">{error}</Alert>
      <div>
        <label className="label" htmlFor="email">Email</label>
        <input id="email" className="input" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
      </div>
      <div>
        <label className="label" htmlFor="password">Password</label>
        <input id="password" className="input" type="password" autoComplete={requireStrongPassword ? "new-password" : "current-password"} value={password} onChange={(e) => setPassword(e.target.value)} />
      </div>
      {requireStrongPassword && (
        <div>
          <label className="label" htmlFor="confirm">Confirm password</label>
          <input id="confirm" className="input" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
      )}
      {requireInviteCode && (
        <div>
          <label className="label" htmlFor="invite_code">Invite code</label>
          <input id="invite_code" name="invite_code" className="input" type="text" autoComplete="off" autoCapitalize="off" spellCheck={false}
                 value={inviteCode} onChange={(e) => setInviteCode(e.target.value)} />
          <p className="mt-1 text-xs text-slate-500">AutoBuy is in private beta. No code? Email support to request access.</p>
        </div>
      )}
      <button className="btn-primary w-full" type="submit" disabled={busy}>{busy ? "Please wait…" : submitLabel}</button>
    </form>
  );
}
