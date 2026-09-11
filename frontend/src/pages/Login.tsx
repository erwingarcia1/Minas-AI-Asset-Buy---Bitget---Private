import { useEffect } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { AuthCard, AuthForm } from "@/components/Auth";
import { Alert } from "@/components/Common";
import { errorMessage } from "@/api/client";
import { useAuth } from "@/hooks/useAuth";
import { useAuthStore } from "@/store/authStore";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const notice = useAuthStore((s) => s.notice);
  const clearNotice = useAuthStore((s) => s.clearNotice);
  const from = (location.state as { from?: string } | null)?.from ?? "/dashboard";

  useEffect(() => () => clearNotice(), [clearNotice]);

  return (
    <AuthCard title="Sign in" subtitle="Automated crypto purchases, on your schedule."
      footer={<>No account yet? <Link className="text-brand-600 hover:underline" to="/signup">Create one</Link>
        <span className="mx-2 text-slate-300">·</span>
        <Link className="text-brand-600 hover:underline" to="/forgot-password">Forgot password?</Link></>}>
      {notice && <div className="mb-4"><Alert kind="warning">{notice}</Alert></div>}
      <AuthForm submitLabel="Sign in" onSubmit={async (email, password) => {
        try {
          await login(email, password);
          navigate(from, { replace: true });
        } catch (e) {
          throw new Error(errorMessage(e));
        }
      }} />
    </AuthCard>
  );
}
