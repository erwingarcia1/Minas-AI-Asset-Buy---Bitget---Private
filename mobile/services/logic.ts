// Pure logic shared by mobile screens — kept free of React Native imports so
// it is unit-testable in a plain node environment.

export function validateEmail(email: string): string | null {
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return "Enter a valid email address.";
  return null;
}

export function validatePassword(password: string): string | null {
  const problems: string[] = [];
  if (password.length < 8) problems.push("at least 8 characters");
  if (!/[a-z]/.test(password)) problems.push("a lowercase letter");
  if (!/[A-Z]/.test(password)) problems.push("an uppercase letter");
  if (!/[0-9]/.test(password)) problems.push("a digit");
  if (!/[^a-zA-Z0-9]/.test(password)) problems.push("a symbol");
  return problems.length ? `Password needs ${problems.join(", ")}.` : null;
}

export function passwordStrength(password: string): 0 | 1 | 2 | 3 {
  let score = 0;
  if (password.length >= 8) score++;
  if (password.length >= 12) score++;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)
      && /[0-9]/.test(password) && /[^a-zA-Z0-9]/.test(password)) score++;
  return Math.min(score, 3) as 0 | 1 | 2 | 3;
}

export function validateAmount(value: number): string | null {
  if (Number.isNaN(value)) return "Enter an amount.";
  if (value < 10) return "Minimum is 10 USDT.";
  if (value > 1_000_000) return "Maximum is 1,000,000 USDT.";
  return null;
}

/** Exponential backoff delays (ms) for network retries: 1s, 2s, 4s … capped. */
export function backoffDelays(attempts: number, baseMs = 1000, capMs = 30_000): number[] {
  return Array.from({ length: attempts }, (_, i) => Math.min(baseMs * 2 ** i, capMs));
}

export async function withRetry<T>(
  fn: () => Promise<T>,
  attempts = 3,
  sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
): Promise<T> {
  const delays = backoffDelays(attempts - 1);
  let lastError: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      const status = (err as { response?: { status?: number } }).response?.status;
      if (status !== undefined && status < 500 && status !== 429) throw err; // don't retry client errors
      if (i < delays.length) await sleep(delays[i]);
    }
  }
  throw lastError;
}
