import { lazy, Suspense, useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { bootstrapSession, csrfToken } from "@/api/client";
import { useAuthStore } from "@/store/authStore";
import { purgeLegacyTokenStorage } from "@/store/authStore";
import { ErrorBoundary, Loading, ProtectedRoute, PublicOnlyRoute } from "@/components/Common";
import { MainContainer } from "@/components/Layout";

const Login = lazy(() => import("@/pages/Login"));
const Signup = lazy(() => import("@/pages/Signup"));
const ForgotPassword = lazy(() => import("@/pages/ForgotPassword"));
const ResetPassword = lazy(() => import("@/pages/ResetPassword"));
const Dashboard = lazy(() => import("@/pages/Dashboard"));
const KYC = lazy(() => import("@/pages/KYC"));
const Purchases = lazy(() => import("@/pages/Purchases"));
const Transactions = lazy(() => import("@/pages/Transactions"));
const Settings = lazy(() => import("@/pages/Settings"));
const Admin = lazy(() => import("@/pages/Admin"));
const NotFound = lazy(() => import("@/pages/NotFound"));
const Landing = lazy(() => import("@/pages/Landing"));
const Terms = lazy(() => import("@/pages/Terms"));
const Privacy = lazy(() => import("@/pages/Privacy"));

/** `/`: the landing page for visitors, the dashboard for signed-in users.
 *  A returning session (CSRF cookie present) waits for the silent refresh so
 *  the landing page never flashes before the redirect; a visitor with no
 *  cookie gets the landing page immediately. */
export function Home() {
  const status = useAuthStore((s) => s.status);
  const authed = useAuthStore((s) => Boolean(s.accessToken));
  if (authed) return <Navigate to="/dashboard" replace />;
  if (status === "unknown" && csrfToken() !== null) return <Loading full />;
  return <Landing />;
}

export default function App() {
  // Session bootstrap: drop any tokens an older build left in localStorage, then
  // try the httpOnly refresh cookie once. Route guards wait for the answer.
  useEffect(() => { purgeLegacyTokenStorage(); void bootstrapSession(); }, []);
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Suspense fallback={<Loading full />}>
          <Routes>
            <Route element={<PublicOnlyRoute />}>
              <Route path="/login" element={<Login />} />
              <Route path="/signup" element={<Signup />} />
              <Route path="/forgot-password" element={<ForgotPassword />} />
              <Route path="/reset-password" element={<ResetPassword />} />
            </Route>
            <Route element={<ProtectedRoute />}>
              <Route element={<MainContainer />}>
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/kyc" element={<KYC />} />
                <Route path="/purchases" element={<Purchases />} />
                <Route path="/transactions" element={<Transactions />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="/settings/notifications" element={<Settings />} />
                <Route path="/admin" element={<Admin />} />
                <Route path="*" element={<NotFound />} />
              </Route>
            </Route>
            <Route path="/" element={<Home />} />
            <Route path="/terms" element={<Terms />} />
            <Route path="/privacy" element={<Privacy />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
