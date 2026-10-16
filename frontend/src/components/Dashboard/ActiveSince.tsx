import { useEffect, useState } from "react";
import { useAuthStore } from "@/store/authStore";
import { fmtDay } from "@/utils/format";

const pad = (n: number) => String(n).padStart(2, "0");

/** Whole-second duration since `fromIso`, clamped at zero so a client clock that
 *  runs behind the server never shows a negative age. */
export function activeFor(fromIso: string, nowMs: number): string {
  const total = Math.max(0, Math.floor((nowMs - new Date(fromIso).getTime()) / 1000));
  const days = Math.floor(total / 86400);
  const clock = `${pad(Math.floor((total % 86400) / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
  return days > 0 ? `${days} day${days === 1 ? "" : "s"}, ${clock}` : clock;
}

/** Live counter of how long this account has had AutoBuy, from registration. */
export default function ActiveSince() {
  const user = useAuthStore((s) => s.user);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  if (!user?.created_at) return null;
  return (
    <div className="card flex flex-wrap items-baseline gap-x-2 gap-y-1" data-testid="active-since-clock">
      <span className="text-sm text-slate-500">AutoBuy active for</span>
      <span className="font-mono text-lg font-semibold tabular-nums text-slate-900">{activeFor(user.created_at, now)}</span>
      <span className="text-xs text-slate-400">since you registered on {fmtDay(user.created_at)}</span>
    </div>
  );
}
