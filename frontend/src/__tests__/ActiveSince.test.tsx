import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ActiveSince, { activeFor } from "@/components/Dashboard/ActiveSince";
import { useAuthStore } from "@/store/authStore";

const USER = { id: "u1", email: "a@example.com", kyc_status: "verified", created_at: "2026-10-12T10:00:00Z" };

describe("ActiveSince: the counting clock since registration", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-14T13:04:05Z"));
    useAuthStore.setState({ user: USER } as never);
  });
  afterEach(() => {
    vi.useRealTimers();
    useAuthStore.setState({ user: null } as never);
  });

  it("shows days plus a ticking HH:MM:SS and the registration date", () => {
    render(<ActiveSince />);
    const clock = screen.getByTestId("active-since-clock");
    expect(clock).toHaveTextContent("AutoBuy active for");
    expect(clock).toHaveTextContent("2 days, 03:04:05");
    expect(clock).toHaveTextContent(/since you registered on/);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(clock).toHaveTextContent("2 days, 03:04:06");
    act(() => { vi.advanceTimersByTime(54_000); });
    expect(clock).toHaveTextContent("2 days, 03:05:00");
  });

  it("renders nothing while no user is loaded", () => {
    useAuthStore.setState({ user: null } as never);
    render(<ActiveSince />);
    expect(screen.queryByTestId("active-since-clock")).toBeNull();
  });

  it("activeFor never goes negative and pluralises days", () => {
    const t0 = new Date("2026-10-14T00:00:00Z").getTime();
    expect(activeFor("2026-10-14T00:00:30Z", t0)).toBe("00:00:00"); // client clock behind the server
    expect(activeFor("2026-10-13T23:59:10Z", t0)).toBe("00:00:50");
    expect(activeFor("2026-10-13T00:00:00Z", t0)).toBe("1 day, 00:00:00");
    expect(activeFor("2026-09-14T22:30:01Z", t0)).toBe("29 days, 01:29:59");
  });
});
