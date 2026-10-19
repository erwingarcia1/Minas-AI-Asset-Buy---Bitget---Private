import { useAuth } from "@/hooks/useAuth";

/** Is Guardian (read-only exchange monitoring) visible to this user? Mirrors the server's rule, flag on
 *  and, when an allowlist is set, the user on it, as reported by the profile. Cosmetic only: every
 *  /api/guardian route answers 404 on its own while the feature is off. */
export function useGuardian(): boolean {
  const { user } = useAuth();
  return user?.guardian_enabled === true;
}
