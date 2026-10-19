import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { AuthCard, AuthForm, MfaCodeForm } from "@/components/Auth";
import { Alert } from "@/components/Common";
import { errorMessage } from "@/api/client";
import { useAuth } from "@/hooks/useAuth";
import { useAuthStore } from "@/store/authStore";

export default function Login() {
  const { login, completeMfa } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const notice = useAuthStore((s) => s.notice);
  const clearNotice = useAuthStore((s) => s.clearNotice);
  const from = (location.state as { from?: string } | null)?.from ?? "/dashboard";
  // Held in component state only, never stored: it is not a credential and dies in five minutes.
  const [mfaToken, setMfaToken] = useState<string | null>(null);

  useEffect(() => () => clearNotice(), [clearNotice]);

  if (mfaToken) {
    return (
      <AuthCard title="Enter your code" subtitle="Two-factor authentication is on for this account.">
        <MfaCodeForm
          onSubmit={async (factor) => {
            try {
              await completeMfa(mfaToken, factor);
              navigate(from, { replace: true });
            } catch (e) {
              throw new Error(errorMessage(e));
            }
          }}
          onBack={() => setMfaToken(null)}
        />
      </AuthCard>
    );
  }

  return (
    <AuthCard title="Sign in" subtitle="Automated crypto purchases, on your schedule."
      footer={<>No account yet? <Link className="text-brand-600 hover:underline" to="/signup">Create one</Link>
        <span className="mx-2 text-slate-300">·</span>
        <Link className="text-brand-600 hover:underline" to="/forgot-password">Forgot password?</Link></>}>
      {notice && <div className="mb-4"><Alert kind="warning">{notice}</Alert></div>}
      <AuthForm submitLabel="Sign in" onSubmit={async (email, password) => {
        try {
          const result = await login(email, password);
          if (result.mfaToken) setMfaToken(result.mfaToken);
          else navigate(from, { replace: true });
        } catch (e) {
          throw new Error(errorMessage(e));
        }
      }} />
    </AuthCard>
  );
}
