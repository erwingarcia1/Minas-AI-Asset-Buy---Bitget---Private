const colors: Record<string, string> = {
  verified: "bg-green-100 text-green-800",
  completed: "bg-green-100 text-green-800",
  pending: "bg-amber-100 text-amber-800",
  rejected: "bg-red-100 text-red-800",
  failed: "bg-red-100 text-red-800",
  active: "bg-brand-100 text-brand-800",
  paused: "bg-slate-100 text-slate-700",
  autobuy: "bg-brand-100 text-brand-800",
  accumulate: "bg-violet-100 text-violet-800",
  success: "bg-green-100 text-green-800",
  cancelled: "bg-slate-100 text-slate-500",
  needs_review: "bg-red-100 text-red-800",
  withdrawal_failed: "bg-red-100 text-red-800",
  needs_info: "bg-amber-100 text-amber-800",
  simulated: "bg-sky-100 text-sky-800",
  suspended: "bg-red-100 text-red-800",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${colors[status] ?? "bg-slate-100 text-slate-700"}`}>{status.replace(/_/g, " ")}</span>;
}
