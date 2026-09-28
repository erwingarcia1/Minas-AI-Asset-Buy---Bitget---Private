import { Link, useNavigate } from "react-router-dom";
import { AuthCard, AuthForm } from "@/components/Auth";
import { errorMessage } from "@/api/client";
import { useAuth } from "@/hooks/useAuth";

export default function Signup() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  return (
    <AuthCard title="Create your account" subtitle="You'll verify your identity before the first purchase runs."
      footer={<>Already registered? <Link className="text-brand-600 hover:underline" to="/login">Sign in</Link></>}>
      <AuthForm submitLabel="Create account" requireStrongPassword requireInviteCode onSubmit={async (email, password, inviteCode) => {
        try {
          await signup(email, password, inviteCode);
          navigate("/kyc", { replace: true });
        } catch (e) {
          throw new Error(errorMessage(e));
        }
      }} />
    </AuthCard>
  );
}
