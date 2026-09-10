import { FormEvent, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { auth } from "../services/api";

/** Mirrors the API's password policy. */
export function passwordProblem(pw: string): string | null {
  if (pw.length < 8) return "Password must be at least 8 characters.";
  const missing = [[/[a-z]/, "a lowercase letter"], [/[A-Z]/, "an uppercase letter"], [/\d/, "a digit"], [/[^A-Za-z0-9]/, "a special character"]]
    .filter(([re]) => !(re as RegExp).test(pw)).map(([, l]) => l);
  return missing.length ? `Password must contain ${missing.join(", ")}.` : null;
}

export default function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(!token);
  const [checking, setChecking] = useState(Boolean(token));
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  // Validate the link before showing the form so a used or expired link is
  // reported immediately; the server re-checks on submit regardless.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    auth.validateResetToken(token)
      .then((r) => { if (!cancelled && !r.valid) setInvalid(true); })
      .catch((err: unknown) => {
        const status = (err as { response?: { status?: number } }).response?.status;
        if (!cancelled && status === 400) setInvalid(true);
      })
      .finally(() => { if (!cancelled) setChecking(false); });
    return () => { cancelled = true; };
  }, [token]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const p = passwordProblem(password);
    if (p) return setError(p);
    if (password !== confirm) return setError("Passwords do not match.");
    setError(null);
    setBusy(true);
    try {
      await auth.resetPassword(token, password);
      setDone(true);
    } catch (err: unknown) {
      const res = (err as { response?: { status?: number; data?: { detail?: unknown } } }).response;
      if (res?.status === 400) setInvalid(true);
      else setError(typeof res?.data?.detail === "string" ? res.data.detail : "Could not reset the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 p-4">
      <div className="w-full max-w-sm space-y-4 rounded-lg bg-white p-6 shadow-sm">
        {checking ? (
          <p role="status" className="text-sm text-gray-500">Checking your reset link…</p>
        ) : invalid ? (
          <>
            <h1 className="text-xl font-semibold">This reset link is no longer valid</h1>
            <p className="text-sm text-gray-600">Links expire after 1 hour and can only be used once.</p>
            <Link to="/forgot-password" className="block w-full rounded bg-blue-600 px-4 py-2 text-center text-white">Request a new link</Link>
          </>
        ) : done ? (
          <>
            <h1 className="text-xl font-semibold">Password updated</h1>
            <p role="status" className="rounded bg-green-50 p-3 text-sm text-green-800">Your new password is set and every other session has been signed out.</p>
            <Link to="/login" className="block w-full rounded bg-blue-600 px-4 py-2 text-center text-white">Sign in</Link>
          </>
        ) : (
          <>
            <h1 className="text-xl font-semibold">Choose a new password</h1>
            <form onSubmit={submit} className="space-y-3">
              {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
              <label className="block text-sm">New password
                <input type="password" required autoComplete="new-password" className="mt-1 w-full rounded border px-3 py-2"
                       value={password} onChange={(e) => setPassword(e.target.value)} />
              </label>
              <label className="block text-sm">Confirm new password
                <input type="password" required autoComplete="new-password" className="mt-1 w-full rounded border px-3 py-2"
                       value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </label>
              <button type="submit" disabled={busy} className="w-full rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50">
                {busy ? "Saving…" : "Set new password"}
              </button>
            </form>
          </>
        )}
        <p className="text-sm text-gray-600"><Link to="/login" className="text-blue-600 underline">Back to sign in</Link></p>
      </div>
    </div>
  );
}
