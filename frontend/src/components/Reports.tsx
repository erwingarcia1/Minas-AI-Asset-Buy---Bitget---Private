import { useEffect, useState } from "react";
import { api } from "../services/api";

interface Performance {
  has_data: boolean; price_available?: boolean; total_invested_usdt?: number;
  current_value_usdt?: number; roi_pct?: number; annualized_roi_pct?: number;
  purchase_count?: number; first_purchase?: string;
}

export default function Reports() {
  const [perf, setPerf] = useState<Performance | null>(null);
  const year = new Date().getFullYear();

  useEffect(() => {
    api.get<Performance>("/api/reports/performance").then((r) => setPerf(r.data))
      .catch(() => setPerf({ has_data: false }));
  }, []);

  async function download(path: string, filename: string) {
    const { data } = await api.get(path, { responseType: "blob" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(data as Blob);
    a.download = filename;
    a.click();
  }

  return (
    <section className="rounded-lg bg-white p-4 shadow-sm">
      <h2 className="mb-3 font-medium">Reports</h2>
      {perf?.has_data ? (
        <p className="text-sm text-gray-600">
          {perf.purchase_count} purchases since {perf.first_purchase} ·
          invested ${perf.total_invested_usdt?.toLocaleString()}
          {perf.roi_pct != null && <> · ROI <b className={perf.roi_pct >= 0
            ? "text-teal-700" : "text-red-700"}>{perf.roi_pct}%</b>
            {" "}({perf.annualized_roi_pct}% annualized)</>}
        </p>
      ) : (
        <p className="text-sm text-gray-500">Reports appear after your first purchase.</p>
      )}
      <div className="mt-3 flex gap-2">
        <button onClick={() => download(`/api/reports/tax-summary?year=${year}&format=csv`,
                                        `autobuy-tax-${year}.csv`)}
                className="rounded border px-3 py-1.5 text-sm">
          Tax CSV ({year})
        </button>
        <button onClick={() => download(`/api/reports/export?year=${year}`,
                                        `autobuy-report-${year}.pdf`)}
                className="rounded border px-3 py-1.5 text-sm">
          PDF report
        </button>
      </div>
    </section>
  );
}
