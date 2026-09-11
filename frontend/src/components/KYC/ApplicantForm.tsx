import { useState, type FormEvent } from "react";
import { Alert } from "@/components/Common";
import { errorMessage } from "@/api/client";
import { kycAPI } from "@/api/endpoints";
import type { CreateApplicantResponse } from "@/types";

/** Creates the Sumsub applicant with the user's legal name (email is fixed to the account). */
export function ApplicantForm({ email, onCreated }: { email: string; onCreated: (r: CreateApplicantResponse) => void }) {
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!first.trim() || !last.trim()) return setError("Enter your legal first and last name");
    setBusy(true);
    setError(null);
    try {
      onCreated(await kycAPI.createApplicant({ email, first_name: first.trim(), last_name: last.trim() }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4">
      <h2 className="font-semibold">Start verification</h2>
      <p className="text-sm text-slate-600">Use the name exactly as it appears on your government ID.</p>
      <Alert kind="error">{error}</Alert>
      <div className="grid gap-4 sm:grid-cols-2">
        <div><label className="label">First name</label><input className="input" value={first} onChange={(e) => setFirst(e.target.value)} /></div>
        <div><label className="label">Last name</label><input className="input" value={last} onChange={(e) => setLast(e.target.value)} /></div>
      </div>
      <div><label className="label">Email</label><input className="input" value={email} readOnly /></div>
      <button className="btn-primary" type="submit" disabled={busy}>{busy ? "Creating…" : "Create applicant"}</button>
    </form>
  );
}
