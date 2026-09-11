import { StatusBadge } from "@/components/Common";
import type { KycStatusResponse } from "@/types";

export type KycUiState = "not_started" | "in_progress" | "under_review" | "verified" | "rejected";

/** Collapse the API's stored status + Sumsub review state into one UI state. */
export function uiState(s: KycStatusResponse | null, sdkOpen: boolean): KycUiState {
  if (!s) return "not_started";
  if (s.kyc_status === "verified") return "verified";
  if (s.kyc_status === "rejected") return "rejected";
  if (sdkOpen) return "in_progress";
  if (s.review_status === "pending") return "under_review";
  return "not_started";
}

const copy: Record<KycUiState, { badge: string; title: string; text: string }> = {
  not_started: { badge: "pending", title: "Not started", text: "Identity verification is required before any scheduled purchase can execute." },
  in_progress: { badge: "pending", title: "In progress", text: "Follow the steps in the verification widget below. Your progress is saved." },
  under_review: { badge: "pending", title: "Submitted, under review", text: "Your documents were received. Review usually completes within minutes; this page updates automatically." },
  verified: { badge: "verified", title: "Verified", text: "Your identity is verified. Scheduled purchases can execute." },
  rejected: { badge: "rejected", title: "Rejected", text: "Your verification was not approved." },
};

export function KycStatusCard({ status, state }: { status: KycStatusResponse | null; state: KycUiState }) {
  const c = copy[state];
  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">Verification status: {c.title}</h2>
        <StatusBadge status={c.badge} />
      </div>
      <p className="mt-2 text-sm text-slate-600">{c.text}</p>
      {state === "rejected" && (
        <div className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-800">
          <p className="font-medium">Reason</p>
          <p>{status?.rejection_reason || (status?.reject_labels?.length ? status.reject_labels.join(", ").replace(/_/g, " ").toLowerCase() : "No reason was provided. Press \"Check details\" to fetch it.")}</p>
          {status?.retryable === false && <p className="mt-1">This decision is final. Contact support if you believe it is a mistake.</p>}
        </div>
      )}
    </div>
  );
}
