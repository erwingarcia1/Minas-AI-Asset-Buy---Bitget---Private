import { useState } from "react";
import axios from "axios";
import { reportsAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert } from "@/components/Common";
import { EstimatesNote } from "@/components/Dashboard/Analytics";
import { useAsync } from "@/hooks/useAsync";
import { fmtDay, fmtUsdt } from "@/utils/format";

/** Hand a Blob to the browser as a download. */
function saveBlob(blob: Blob, filename: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

export default function Reports() {
  const { data: perf, error } = useAsync(() => reportsAPI.performance());
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState<"csv" | "pdf" | null>(null);
  const year = new Date().getFullYear();

  async function download(kind: "csv" | "pdf") {
    setMsg(null); setBusy(kind);
    try {
      const blob = kind === "csv" ? await reportsAPI.taxCsv(year) : await reportsAPI.exportPdf(year);
      saveBlob(blob, kind === "csv" ? `autobuy-tax-${year}.csv` : `autobuy-report-${year}.pdf`);
    } catch (err) {
      if (axios.isAxiosError(err) && err.response?.status === 404) setMsg(`No completed purchases in ${year} yet.`);
      else setMsg(errorMessage(err, "Could not download the report"));
    } finally { setBusy(null); }
  }

  return (
    <section className="card space-y-3" aria-label="Reports">
      <h2 className="font-semibold">Reports</h2>
      {error && <Alert kind="error">{error}</Alert>}
      {perf?.has_data ? (
        <p className="text-sm text-slate-600">
          {perf.purchase_count} purchases since {fmtDay(perf.first_purchase)} · invested {fmtUsdt(perf.total_invested_usdt)}
          {perf.roi_pct != null && (
            <> · ROI <b className={perf.roi_pct >= 0 ? "text-green-700" : "text-red-700"}>{perf.roi_pct}%</b>
              {perf.annualized_roi_pct != null && <> ({perf.annualized_roi_pct}% annualized)</>}</>
          )}
        </p>
      ) : (
        <p className="text-sm text-slate-500">Reports appear after your first completed purchase.</p>
      )}
      <EstimatesNote />
      {msg && <Alert kind="info">{msg}</Alert>}
      <div className="flex flex-wrap gap-2">
        <button className="btn-secondary" disabled={busy !== null} onClick={() => void download("csv")}>{busy === "csv" ? "Preparing…" : `Tax CSV (${year})`}</button>
        <button className="btn-secondary" disabled={busy !== null} onClick={() => void download("pdf")}>{busy === "pdf" ? "Preparing…" : `PDF report (${year})`}</button>
      </div>
    </section>
  );
}
