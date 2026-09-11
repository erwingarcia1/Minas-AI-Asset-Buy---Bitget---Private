import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { Navbar } from "./Navbar";
import { Sidebar } from "./Sidebar";
import { useAuth } from "@/hooks/useAuth";

/** Authenticated shell: navbar + sidebar + routed page content. */
export function MainContainer() {
  const [open, setOpen] = useState(false);
  const { user, refreshProfile } = useAuth();

  useEffect(() => {
    if (!user) refreshProfile().catch(() => undefined);
  }, [user, refreshProfile]);

  return (
    <div className="min-h-screen bg-slate-50">
      <Navbar onToggleSidebar={() => setOpen((v) => !v)} />
      <div className="flex">
        <Sidebar open={open} onClose={() => setOpen(false)} />
        <main className="min-w-0 flex-1 p-4 md:p-8">
          <div className="mx-auto max-w-6xl"><Outlet /></div>
        </main>
      </div>
    </div>
  );
}
