import { NavLink, useLocation } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import { useGuardian } from "@/hooks/useGuardian";

const links = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/purchases", label: "Purchases" },
  { to: "/transactions", label: "Transactions" },
  { to: "/kyc", label: "Verification" },
  { to: "/settings", label: "Settings" },
];

export function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const isAdmin = useAuthStore((s) => s.user?.role === "admin");
  const guardian = useGuardian();
  const { pathname } = useLocation();
  // Cosmetic only: every /api/admin/* route enforces the role on the server, and every
  // /api/guardian/* route answers 404 while the feature is off for this user.
  const visible = [
    ...links,
    ...(guardian ? [{ to: "/settings/monitoring", label: "Guardian" }] : []),
    ...(isAdmin ? [{ to: "/admin", label: "Admin" }] : []),
  ];
  // Guardian's deep link lives under /settings for now: light that entry alone, not Settings as well.
  const lit = (to: string, isActive: boolean) => (to === "/settings" ? isActive && !pathname.startsWith("/settings/monitoring") : isActive);
  return (
    <>
      {open && <div className="fixed inset-0 z-20 bg-black/30 md:hidden" onClick={onClose} />}
      <aside className={`fixed inset-y-0 left-0 z-30 w-60 transform border-r border-slate-200 bg-white pt-14 transition-transform md:static md:translate-x-0 md:pt-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <nav className="flex flex-col gap-1 p-3">
          {visible.map((l) => (
            <NavLink key={l.to} to={l.to} onClick={onClose}
              className={({ isActive }) => `rounded-md px-3 py-2 text-sm font-medium ${lit(l.to, isActive) ? "bg-brand-50 text-brand-700" : "text-slate-700 hover:bg-slate-100"}`}>
              {l.label}
            </NavLink>
          ))}
        </nav>
      </aside>
    </>
  );
}
