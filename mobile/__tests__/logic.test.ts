import { describe, expect, it } from "vitest";
import {
  backoffDelays, passwordStrength, validateAmount, validateEmail,
  validatePassword, withRetry,
} from "../services/logic";

describe("validation", () => {
  it("emails", () => {
    expect(validateEmail("a@b.co")).toBeNull();
    expect(validateEmail("not-an-email")).toMatch(/valid email/);
  });
  it("passwords", () => {
    expect(validatePassword("Str0ng!pass")).toBeNull();
    expect(validatePassword("weak")).toMatch(/at least 8/);
    expect(validatePassword("alllowercase1!")).toMatch(/uppercase/);
  });
  it("strength scale", () => {
    expect(passwordStrength("weak")).toBe(0);
    expect(passwordStrength("Str0ng!pass4long")).toBe(3);
  });
  it("amounts mirror the API bounds", () => {
    expect(validateAmount(9)).toMatch(/Minimum/);
    expect(validateAmount(10)).toBeNull();
    expect(validateAmount(1_000_001)).toMatch(/Maximum/);
  });
});

describe("retry/backoff", () => {
  it("delays grow exponentially and cap", () => {
    expect(backoffDelays(4)).toEqual([1000, 2000, 4000, 8000]);
    expect(backoffDelays(7).at(-1)).toBe(30_000);
  });
  it("retries server errors then succeeds", async () => {
    let calls = 0;
    const result = await withRetry(async () => {
      calls += 1;
      if (calls < 3) throw { response: { status: 503 } };
      return "ok";
    }, 3, async () => undefined);
    expect(result).toBe("ok");
    expect(calls).toBe(3);
  });
  it("does not retry client errors", async () => {
    let calls = 0;
    await expect(withRetry(async () => {
      calls += 1;
      throw { response: { status: 422 } };
    }, 3, async () => undefined)).rejects.toBeTruthy();
    expect(calls).toBe(1);
  });
});
