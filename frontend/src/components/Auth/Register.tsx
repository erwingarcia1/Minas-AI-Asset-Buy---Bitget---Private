import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { auth } from "../../services/api";

export default function Register() {
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
      await auth.register(email, password);
      await auth.login(email, password);
      navigate("/dashboard");
    } catch (err: unknown) {
      const data = (err as { response?: { data?: { detail?: unknown } } }).response?.data;
      const detail = data?.detail;
      setError(typeof detail === "string" ? detail
        : Array.isArray(detail) ? String((detail[0] as { msg?: string })?.msg ?? "Invalid input")
        : "Registration failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg bg-white p-8 shadow">
      <h1 className="text-2xl font-semibold">Create your account</h1>
      {error && <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
      <label className="block text-sm">
        Email
        <input type="email" required value={email} autoComplete="email"
               onChange={(e) => setEmail(e.target.value)}
               className="mt-1 w-full rounded border p-2" />
      </label>
      <label className="block text-sm">
        Password
        <input type="password" required minLength={8} value={password} autoComplete="new-password"
               onChange={(e) => setPassword(e.target.value)}
               className="mt-1 w-full rounded border p-2" />
        <span className="text-xs text-gray-500">
          At least 8 characters with upper- and lowercase letters, a digit, and a symbol.
        </span>
      </label>
      <button disabled={busy}
              className="w-full rounded bg-blue-600 p-2 font-medium text-white disabled:opacity-50">
        {busy ? "Creating…" : "Create account"}
      </button>
      <p className="text-sm text-gray-600">
        Already registered? <Link to="/login" className="text-blue-600 underline">Sign in</Link>
      </p>
    </form>
  );
}
