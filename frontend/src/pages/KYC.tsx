import { useCallback, useEffect, useRef, useState } from "react";
import { kycAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading } from "@/components/Common";
import { ApplicantForm } from "@/components/KYC";
import { KycStatusCard, uiState } from "@/components/KYC/KycStatusCard";
import SumsubVerification from "@/components/KYC/SumsubVerification";
import { useAuth } from "@/hooks/useAuth";
import type { KycStatusResponse } from "@/types";

const REVIEW_POLL_MS = 20_000;

export default function KYC() {
  const { user, refreshProfile } = useAuth();
  const [status, setStatus] = useState<KycStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);   // held in state only; never rendered
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false);                             // guards double clicks across renders

  const load = useCallback(async (sync = false) => {
    try {
      const s = await kycAPI.status(sync);
      setStatus(s);
      setError(null);
      if (s.kyc_status !== "pending") setToken(null);        // final: close the widget
      return s;
    } catch (e) {
      setError(errorMessage(e));
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(false); }, [load]);           // page load: stored status, synced only while pending

  const state = uiState(status, token !== null);

  // While under review, poll (synced) so a decision shows up without the webhook.
  useEffect(() => {
    if (state !== "under_review") return;
    const id = setInterval(() => void load(true), REVIEW_POLL_MS);
    return () => clearInterval(id);
  }, [state, load]);

  async function start() {
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setError(null);
    try {
      const s = await kycAPI.initiate();
      setToken(s.access_token);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
      inflight.current = false;
    }
  }

  async function onApplicantEvent(type: string) {
    const s = await load(true);
    if (s && s.kyc_status === "verified") await refreshProfile().catch(() => undefined);
    if (s && s.kyc_status === "pending" && type === "idCheck.onApplicantSubmitted") setToken(null); // leave the widget, show "under review"
  }

  if (loading || !user) return <Loading />;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Identity verification</h1>
      <KycStatusCard status={status} state={state} />
      {error && <Alert kind="error">{error}</Alert>}

      {state !== "verified" && !status?.applicant_id && (
        <ApplicantForm email={user.email} onCreated={() => void load(false)} />
      )}

      {status?.applicant_id && (state === "not_started" || state === "rejected") && (
        <div className="card space-y-3">
          <h2 className="font-semibold">{state === "rejected" ? "Try again" : "Verify your identity"}</h2>
          <p className="text-sm text-slate-600">
            {state === "rejected"
              ? "Fix the issue above and resubmit your documents."
              : "You will be asked for a government ID and a short selfie check. It takes about two minutes."}
          </p>
          <div className="flex flex-wrap gap-3">
            {(state !== "rejected" || status.retryable !== false) && (
              <button className="btn-primary" onClick={() => void start()} disabled={busy}>
                {busy ? "Starting…" : state === "rejected" ? "Retry verification" : "Start verification"}
              </button>
            )}
            <button className="btn-secondary" onClick={() => void load(true)} disabled={busy}>
              {state === "rejected" ? "Check details" : "Refresh status"}
            </button>
          </div>
        </div>
      )}

      {state === "under_review" && (
        <div className="card flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-slate-600">Checking for a decision every 20 seconds.</p>
          <button className="btn-secondary" onClick={() => void load(true)}>Refresh status</button>
        </div>
      )}

      {token && state === "in_progress" && (
        <div className="space-y-3">
          <SumsubVerification accessToken={token} onApplicantEvent={(t) => void onApplicantEvent(t)} />
          <button className="btn-secondary" onClick={() => setToken(null)}>Close widget</button>
        </div>
      )}
    </div>
  );
}
