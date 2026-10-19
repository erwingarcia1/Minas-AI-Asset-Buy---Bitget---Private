import { useEffect, useState } from "react";
import { autoBuyAPI } from "@/api/endpoints";
import { Alert, Loading } from "@/components/Common";
import WalletHoldNotice from "@/components/Common/WalletHoldNotice";
import { useAuth } from "@/hooks/useAuth";
import PurchaseForm from "@/components/Purchases/PurchaseForm";
import PurchaseList from "@/components/Purchases/PurchaseList";
import { useAsync } from "@/hooks/useAsync";

/** One form and one list for both modes: withdraw immediately (Auto-Buy) or accumulate on the exchange (DCA). */
export default function Purchases() {
  const { data: purchases, loading, error, reload } = useAsync(() => autoBuyAPI.list());
  const [actionError, setActionError] = useState<string | null>(null);
  const { user, refreshProfile } = useAuth();
  useEffect(() => { void refreshProfile().catch(() => undefined); }, [refreshProfile]);   // picks up an active wallet-change hold

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Purchases</h1>
      <WalletHoldNotice user={user} />
      <PurchaseForm onCreated={() => void reload()} />
      <div>
        <h2 className="text-lg font-semibold">Your purchases</h2>
        <p className="mt-1 text-sm text-slate-500">Both modes appear here. Open a row to see every execution attempt.</p>
      </div>
      {actionError && <Alert kind="error">{actionError}</Alert>}
      {loading ? <Loading /> : error ? <Alert kind="error">{error}</Alert> : (
        <PurchaseList purchases={purchases ?? []} onChanged={() => { setActionError(null); void reload(); }} onError={setActionError} />
      )}
    </div>
  );
}
