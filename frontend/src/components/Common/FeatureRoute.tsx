import { Navigate, Outlet } from "react-router-dom";
import { Loading } from "./Loading";
import { useAuth } from "@/hooks/useAuth";
import { useGuardian } from "@/hooks/useGuardian";

/** Child routes of the Guardian area: rendered when the feature is on for this user, otherwise the
 *  dashboard. While the profile is still loading there is nothing to decide yet, so a loader rather than a
 *  bounce on a deep link. The server enforces the flag on every Guardian route regardless. */
export function FeatureRoute() {
  const { user } = useAuth();
  const enabled = useGuardian();
  if (!user) return <Loading full />;
  if (!enabled) return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}
