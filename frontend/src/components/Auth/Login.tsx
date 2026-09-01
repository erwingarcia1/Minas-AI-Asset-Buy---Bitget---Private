import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { auth } from "../../services/api";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.login(email, password);
      navigate("/dashboard");
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })
        .response?.data?.detail;
      setError(detail ?? "Login failed — check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg bg-white p-8 shadow">
      <h1 className="text-2xl font-semibold">Sign in to AutoBuy</h1>
      {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
      <label className="block text-sm">
        Email
        <input type="email" required value={email} autoComplete="email"
               onChange={(e) => setEmail(e.target.value)}
               className="mt-1 w-full rounded border p-2" />
      </label>
      <label className="block text-sm">
        Password
        <input type="password" required value={password} autoComplete="current-password"
               onChange={(e) => setPassword(e.target.value)}
               className="mt-1 w-full rounded border p-2" />
      </label>
      <button disabled={busy}
              className="w-full rounded bg-blue-600 p-2 font-medium text-white disabled:opacity-50">
        {busy ? "Signing in…" : "Sign in"}
      </button>
      <p className="text-sm text-gray-600">
        New here? <Link to="/register" className="text-blue-600 underline">Create an account</Link>
      </p>
    </form>
  );
}
