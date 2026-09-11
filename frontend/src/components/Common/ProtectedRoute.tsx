import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";

/** Renders child routes only for authenticated users; otherwise sends to /login. */
export function ProtectedRoute() {
  const authed = useAuthStore((s) => Boolean(s.accessToken));
  const location = useLocation();
  if (!authed) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

/** The inverse: keeps signed-in users away from /login and /signup. */
export function PublicOnlyRoute() {
  const authed = useAuthStore((s) => Boolean(s.accessToken));
  if (authed) return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}
