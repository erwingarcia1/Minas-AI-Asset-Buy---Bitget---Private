import { useCallback, useState } from "react";
import SumsubWebSdk from "@sumsub/websdk-react";
import { kycAPI } from "@/api/endpoints";
import { Alert } from "@/components/Common";

interface Props {
  /** Token from POST /api/kyc/initiate. Never rendered; passed to the SDK only. */
  accessToken: string;
  /** Called on applicant submitted / status changed / review complete so the page re-syncs status. */
  onApplicantEvent: (type: string) => void;
}

const STATUS_EVENTS = new Set([
  "idCheck.onApplicantSubmitted", "idCheck.onApplicantResubmitted",
  "idCheck.onApplicantStatusChanged", "idCheck.onApplicantReviewComplete",
]);

/** Official Sumsub WebSDK (@sumsub/websdk-react) mounted in-page. */
export default function SumsubVerification({ accessToken, onApplicantEvent }: Props) {
  const [sdkError, setSdkError] = useState<string | null>(null);

  // The SDK calls this when the access token expires: fetch a fresh one from our API.
  const expirationHandler = useCallback(async () => (await kycAPI.initiate()).access_token, []);

  return (
    <div className="space-y-3">
      {sdkError && <Alert kind="error">{sdkError}</Alert>}
      <div className="min-h-[560px] overflow-hidden rounded-md border border-slate-200 bg-white" data-testid="sumsub-container">
        <SumsubWebSdk
          accessToken={accessToken}
          expirationHandler={expirationHandler}
          config={{ lang: "en" }}
          options={{ addViewportTag: false, adaptIframeHeight: true }}
          onMessage={(type: string) => {
            if (STATUS_EVENTS.has(type)) onApplicantEvent(type);
          }}
          onError={(err: unknown) => {
            const e = err as { reason?: string; error?: string; code?: string };
            setSdkError(`Verification widget error: ${e?.reason || e?.error || e?.code || "unknown"}`);
          }}
        />
      </div>
    </div>
  );
}
