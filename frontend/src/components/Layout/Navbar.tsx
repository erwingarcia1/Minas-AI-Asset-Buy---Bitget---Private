import { Link } from "react-router-dom";
import { useAuth } from "@/hooks/useAuth";
import { StatusBadge } from "@/components/Common";

export function Navbar({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const { user, logout } = useAuth();
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-slate-200 bg-white px-4">
      <div className="flex items-center gap-3">
        <button className="rounded p-2 text-slate-600 hover:bg-slate-100 md:hidden" onClick={onToggleSidebar} aria-label="Toggle navigation">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 5h14M3 10h14M3 15h14" /></svg>
        </button>
        <Link to="/dashboard" className="flex items-center gap-2 font-semibold text-slate-900">
          <img src="/favicon.svg" alt="" className="h-7 w-7" /> AutoBuy
        </Link>
      </div>
      <div className="flex items-center gap-4 text-sm">
        {user && (
          <span className="hidden items-center gap-2 text-slate-600 sm:flex">
            {user.email} <StatusBadge status={user.kyc_status} />
          </span>
        )}
        <button className="btn-secondary" onClick={() => void logout()}>Sign out</button>
      </div>
    </header>
  );
}
