export const isEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim());

/** Mirrors the API's password policy: 8–128 chars with lower, upper, digit and special. */
export function passwordProblem(pw: string): string | null {
  if (pw.length < 8) return "Password must be at least 8 characters";
  if (pw.length > 128) return "Password must be at most 128 characters";
  const missing = [
    [/[a-z]/, "a lowercase letter"], [/[A-Z]/, "an uppercase letter"], [/\d/, "a digit"], [/[^A-Za-z0-9]/, "a special character"],
  ].filter(([re]) => !(re as RegExp).test(pw)).map(([, label]) => label);
  return missing.length ? `Password must contain ${missing.join(", ")}` : null;
}

/** Recovery codes are 20 hex characters in four groups of five (XXXXX-XXXXX-XXXXX-XXXXX, 23 with the
 *  hyphens). The 14-character codes of the first release (XXXX-XXXX-XXXX) stay valid, so inputs accept
 *  both and only the upper bound is enforced here. */
export const RECOVERY_CODE_MAX_LENGTH = 23;
export const RECOVERY_CODE_PLACEHOLDER = "XXXXX-XXXXX-XXXXX-XXXXX";
