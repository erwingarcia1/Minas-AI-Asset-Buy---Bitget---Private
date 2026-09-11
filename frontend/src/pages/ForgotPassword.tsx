import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { authAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { AuthCard } from "@/components/Auth";
import { Alert } from "@/components/Common";
import { isEmail } from "@/utils/validation";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!isEmail(email)) return setError("Enter a valid email address");
    setBusy(true);
    try {
      await authAPI.forgotPassword(email.trim().toLowerCase());
      setDone(true);
    } catch (err) {
      setError(errorMessage(err, "Could not send the reset email"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Forgot your password?" subtitle="We'll email you a link to choose a new one."
      footer={<Link className="text-brand-600 hover:underline" to="/login">Back to sign in</Link>}>
      {done ? (
        <div className="space-y-4">
          <Alert kind="success">If an account exists for <strong>{email.trim().toLowerCase()}</strong>, a reset link is on its way. It is valid for 1 hour.</Alert>
          <p className="text-sm text-slate-500">Didn't get it? Check your spam folder, or <button className="text-brand-600 hover:underline" onClick={() => setDone(false)}>try again</button>.</p>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-4" noValidate>
          <Alert kind="error">{error}</Alert>
          <div>
            <label className="label" htmlFor="fp-email">Email</label>
            <input id="fp-email" className="input" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
          </div>
          <button className="btn-primary w-full" type="submit" disabled={busy}>{busy ? "Sending…" : "Send reset link"}</button>
        </form>
      )}
    </AuthCard>
  );
}
