import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { auth } from "../services/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await auth.forgotPassword(email.trim().toLowerCase());
      setDone(true);
    } catch {
      setError("Could not send the reset email. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 p-4">
      <div className="w-full max-w-sm space-y-4 rounded-lg bg-white p-6 shadow-sm">
        <h1 className="text-xl font-semibold">Forgot your password?</h1>
        {done ? (
          <p role="status" className="rounded bg-green-50 p-3 text-sm text-green-800">
            If an account exists for <strong>{email.trim().toLowerCase()}</strong>, a reset link is on its way. It is valid for 1 hour.
          </p>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
            <label className="block text-sm">Email
              <input type="email" required autoComplete="email" className="mt-1 w-full rounded border px-3 py-2"
                     value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            <button type="submit" disabled={busy} className="w-full rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50">
              {busy ? "Sending…" : "Send reset link"}
            </button>
          </form>
        )}
        <p className="text-sm text-gray-600"><Link to="/login" className="text-blue-600 underline">Back to sign in</Link></p>
      </div>
    </div>
  );
}
