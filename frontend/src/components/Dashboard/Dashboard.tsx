import { useCallback, useEffect, useState } from "react";
import {
  Bar, BarChart, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { dashboard, transactions, user } from "../../services/api";
import type {
  Dashboard as DashboardData, QuickStats, Timeline, Transaction, User,
} from "../../types";
import ErrorRetry from "../common/ErrorRetry";
import Loading from "../common/Loading";

const PIE_COLORS = ["#2563eb", "#0d9488", "#d97706", "#dc2626", "#7c3aed",
                    "#0891b2", "#65a30d", "#be185d"];

const usd = (n: number) =>
  n.toLocaleString(undefined, { style: "currency", currency: "USD" });

export default function Dashboard() {
  const [data, setData] = useState<{
    profile: User; dash: DashboardData; stats: QuickStats;
    timeline: Timeline; recent: Transaction[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    Promise.all([
      user.getProfile(), dashboard.get(), dashboard.getStats(),
      dashboard.getTimeline(30), transactions.list(10, 0),
    ])
      .then(([profile, dash, stats, timeline, txs]) =>
        setData({ profile, dash, stats, timeline, recent: txs.items }))
      .catch(() => setError("Couldn't load your dashboard."));
  }, []);
  useEffect(load, [load]);

  if (error) return <ErrorRetry message={error} onRetry={load} />;
  if (!data) return <Loading label="Loading your dashboard…" />;
  const { profile, dash, stats, timeline, recent } = data;

  let running = 0;
  const cumulative = timeline.timeline.map((p) => ({
    date: p.date.slice(5), invested: (running += p.total_usdt),
  }));
  const frequencies = Object.entries(
    dash.active_purchases.reduce<Record<string, number>>(
      (acc, p) => ({ ...acc, [p.frequency]: (acc[p.frequency] ?? 0) + 1 }), {}),
  ).map(([frequency, count]) => ({ frequency, count }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Welcome back</h1>
        <p className="text-sm text-gray-500">{profile.email}</p>
      </div>

      <section aria-label="Quick stats" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Total invested" value={usd(stats.total_invested_usdt)} />
        <Stat label="Active schedules" value={String(stats.active_purchases)} />
        <Stat label="Transactions" value={String(stats.transaction_count)} />
        <Stat label="KYC status" value={stats.kyc_status}
              tone={stats.kyc_status === "verified" ? "good" : "warn"} />
      </section>

      <section aria-label="Holdings" className="rounded-lg bg-white p-4 shadow-sm">
        <h2 className="mb-3 font-medium">Holdings</h2>
        {dash.holdings.length === 0 ? (
          <p className="text-sm text-gray-500">No completed purchases yet.</p>
        ) : (
          <div className="grid gap-6 lg:grid-cols-2">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase text-gray-500">
                  <tr><th className="py-1">Crypto</th><th>Amount</th><th>Invested</th></tr>
                </thead>
                <tbody>
                  {dash.holdings.map((h) => (
                    <tr key={h.crypto} className="border-t">
                      <td className="py-2 font-medium">{h.crypto}</td>
                      <td>{h.total_amount.toFixed(8)}</td>
                      <td>{usd(h.total_cost_usdt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="h-56">
              <ResponsiveContainer>
                <PieChart>
                  <Pie data={dash.holdings} dataKey="total_cost_usdt" nameKey="crypto"
                       label={(e) => String((e as { crypto?: string }).crypto ?? "")}>
                    {dash.holdings.map((h, i) => (
                      <Cell key={h.crypto} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(v) => usd(Number(v))} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-label="Investment timeline" className="rounded-lg bg-white p-4 shadow-sm">
          <h2 className="mb-3 font-medium">Invested — last 30 days</h2>
          <div className="h-56">
            <ResponsiveContainer>
              <LineChart data={cumulative}>
                <XAxis dataKey="date" hide />
                <YAxis width={70} tickFormatter={(v) => usd(v)} />
                <Tooltip formatter={(v) => usd(Number(v))} />
                <Line type="monotone" dataKey="invested" stroke="#2563eb" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </section>
        <section aria-label="Purchase frequencies" className="rounded-lg bg-white p-4 shadow-sm">
          <h2 className="mb-3 font-medium">Schedules by frequency</h2>
          <div className="h-56">
            <ResponsiveContainer>
              <BarChart data={frequencies}>
                <XAxis dataKey="frequency" />
                <YAxis allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count" fill="#0d9488" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>

      <section aria-label="Upcoming purchases" className="rounded-lg bg-white p-4 shadow-sm">
        <h2 className="mb-3 font-medium">Next scheduled buys</h2>
        {dash.upcoming_purchases.length === 0 ? (
          <p className="text-sm text-gray-500">Nothing scheduled in the next 30 days.</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            {dash.upcoming_purchases.slice(0, 5).map((p) => (
              <div key={p.id} className="rounded border p-3 text-sm">
                <div className="font-medium">{p.crypto} · {usd(p.amount_usdt)}</div>
                <div className="text-gray-500">{p.frequency}</div>
                <div className="text-gray-500">
                  {new Date(p.next_purchase_date).toLocaleDateString()}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section aria-label="Recent transactions" className="rounded-lg bg-white p-4 shadow-sm">
        <h2 className="mb-3 font-medium">Recent transactions</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-gray-500">
              <tr>
                <th className="py-1">Date</th><th>Crypto</th><th>Amount</th>
                <th>Cost</th><th>Status</th><th>Order</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((t) => (
                <tr key={t.id} className="border-t">
                  <td className="py-2">{new Date(t.created_at).toLocaleString()}</td>
                  <td className="font-medium">{t.crypto}</td>
                  <td>{t.amount_crypto.toFixed(8)}</td>
                  <td>{usd(t.cost_usdt)}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td className="text-gray-500">{t.binance_order_id ?? "—"}</td>
                </tr>
              ))}
              {recent.length === 0 && (
                <tr><td colSpan={6} className="py-4 text-gray-500">No transactions yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "good" | "warn" }) {
  return (
    <div className="rounded-lg bg-white p-4 shadow-sm">
      <div className="text-xs uppercase text-gray-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${
        tone === "good" ? "text-teal-700" : tone === "warn" ? "text-amber-600" : ""}`}>
        {value}
      </div>
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const tones: Record<string, string> = {
    completed: "bg-teal-50 text-teal-700",
    failed: "bg-red-50 text-red-700",
    pending: "bg-amber-50 text-amber-700",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs ${tones[status] ?? "bg-gray-100"}`}>
      {status}
    </span>
  );
}
