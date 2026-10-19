import { Alert } from "@/components/Common";
import type { UserProfile } from "@/types";
import { fmtDate } from "@/utils/format";

const REASON: Record<string, string> = { password_reset: "a password reset", password_change: "a password change" };

/** Shown while the 24-hour hold on new withdrawal addresses is active. */
export default function WalletHoldNotice({ user }: { user: Pick<UserProfile, "wallet_hold_until" | "wallet_hold_reason"> | null }) {
  const until = user?.wallet_hold_until ? new Date(user.wallet_hold_until) : null;
  if (!until || until.getTime() <= Date.now()) return null;
  return (
    <Alert kind="warning">
      <span data-testid="wallet-hold">
        For your safety, new withdrawal addresses are on hold until <b>{fmtDate(user!.wallet_hold_until)}</b> after {REASON[user?.wallet_hold_reason ?? ""] ?? "a security event"}.
        Until then you cannot add a new wallet address, switch a purchase to withdraw immediately, or start an autobuy purchase to an address you have not used before.
        Purchases to addresses you already use keep running.
      </span>
    </Alert>
  );
}
