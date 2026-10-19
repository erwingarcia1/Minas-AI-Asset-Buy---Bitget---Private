import { useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import axios from "axios";
import { authAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { AuthCard } from "@/components/Auth";
import { Alert, Loading } from "@/components/Common";
import { passwordProblem } from "@/utils/validation";

export default function ResetPassword() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const [checking, setChecking] = useState(Boolean(token));
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  // Validate the link before asking for a password, so a used or expired link
  // shows its state immediately. The server re-checks on submit regardless.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    authAPI.validateResetToken(token)
      .then((r) => { if (!cancelled && !r.valid) setExpired(true); })
      .catch((err) => { if (!cancelled && axios.isAxiosError(err) && err.response?.status === 400) setExpired(true); })
      .finally(() => { if (!cancelled) setChecking(false); });
    return () => { cancelled = true; };
  }, [token]);

  const problem = password ? passwordProblem(password) : null;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const p = passwordProblem(password);
    if (p) return setError(p);
    if (password !== confirm) return setError("Passwords do not match");
    setBusy(true);
    try {
      await authAPI.resetPassword(token, password);
      setDone(true);
    } catch (err) {
      if (axios.isAxiosError(err) && err.response?.status === 400) setExpired(true);
      else setError(errorMessage(err, "Could not reset the password"));
    } finally {
      setBusy(false);
    }
  }

  if (!token || expired) {
    // reached by the 1-hour reset link and by the 7-day Guardian activation link alike: no lifetime in the sentence
    return (
      <AuthCard title="This reset link is no longer valid" subtitle="This link has expired or was already used. Request a new one from Forgot password."
        footer={<Link className="text-brand-600 hover:underline" to="/login">Back to sign in</Link>}>
        <Link to="/forgot-password" className="btn-primary w-full">Request a new link</Link>
      </AuthCard>
    );
  }
  if (checking) {
    return (
      <AuthCard title="Checking your reset link…">
        <Loading label="One moment" />
      </AuthCard>
    );
  }
  if (done) {
    return (
      <AuthCard title="Password updated" subtitle="You have been signed out everywhere else.">
        <Alert kind="success">Your new password is set. Sign in to continue.</Alert>
        <Link to="/login" className="btn-primary mt-4 w-full">Sign in</Link>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Choose a new password" subtitle="At least 8 characters with upper and lower case, a digit and a special character."
      footer={<Link className="text-brand-600 hover:underline" to="/login">Back to sign in</Link>}>
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Alert kind="error">{error}</Alert>
        <div>
          <label className="label" htmlFor="rp-password">New password</label>
          <input id="rp-password" className="input" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
          {problem && <p className="mt-1 text-xs text-red-600">{problem}</p>}
        </div>
        <div>
          <label className="label" htmlFor="rp-confirm">Confirm new password</label>
          <input id="rp-confirm" className="input" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        <button className="btn-primary w-full" type="submit" disabled={busy || !password || !confirm}>{busy ? "Saving…" : "Set new password"}</button>
      </form>
    </AuthCard>
  );
}
