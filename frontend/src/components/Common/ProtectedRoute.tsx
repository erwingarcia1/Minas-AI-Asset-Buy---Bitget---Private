import { Navigate, Outlet, useLocation } from "react-router-dom";
import { Loading } from "@/components/Common";
import { useAuthStore } from "@/store/authStore";

/** Renders child routes only for authenticated users; otherwise sends to /login.
 *  While the silent cookie refresh on page load is still pending, shows a loader. */
export function ProtectedRoute() {
  const status = useAuthStore((s) => s.status);
  const authed = useAuthStore((s) => Boolean(s.accessToken));
  const location = useLocation();
  if (status === "unknown") return <Loading full />;
  if (!authed) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

/** The inverse: keeps signed-in users away from /login and /signup. */
export function PublicOnlyRoute() {
  const status = useAuthStore((s) => s.status);
  const authed = useAuthStore((s) => Boolean(s.accessToken));
  if (status === "unknown") return <Loading full />;
  if (authed) return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}
