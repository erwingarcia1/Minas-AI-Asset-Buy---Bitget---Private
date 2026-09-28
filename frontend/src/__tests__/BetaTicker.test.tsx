import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BetaTicker } from "@/components/Common/BetaTicker";
import { BETA_TICKER_TEXT } from "@/config/beta";

function mockReducedMotion(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((q: string) => ({ matches, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  });
}

describe("BetaTicker", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it("scrolls two aria-hidden copies, is read once by screen readers, and pauses with a labelled button", () => {
    mockReducedMotion(false);
    render(<BetaTicker />);
    const region = screen.getByRole("region", { name: "Private beta notice" });
    expect(region).toHaveAttribute("data-moving", "1");
    // exactly one accessible copy of the text (the sr-only paragraph); the lane is aria-hidden
    expect(screen.getByText(BETA_TICKER_TEXT, { selector: "p" })).toBeInTheDocument();
    const lane = region.querySelector(".beta-ticker__lane")!;
    expect(lane.closest("[aria-hidden='true']")).not.toBeNull();
    expect(lane.querySelectorAll(".beta-ticker__item")).toHaveLength(2);
    expect(region.textContent!.split("Private beta").length - 1).toBe(3);   // sr copy + two visual copies
    expect(region.querySelector("button[aria-label*='ismiss']")).toBeNull();
    const btn = screen.getByRole("button", { name: "Pause notice" });
    expect(btn).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(btn);
    expect(screen.getByRole("button", { name: "Play notice" })).toHaveAttribute("aria-pressed", "true");
    expect(lane).toHaveClass("is-paused");
    expect(region).toHaveAttribute("data-moving", "0");
  });

  it("is static under prefers-reduced-motion", () => {
    mockReducedMotion(true);
    render(<BetaTicker />);
    const region = screen.getByRole("region", { name: "Private beta notice" });
    expect(region).toHaveAttribute("data-moving", "0");
    expect(region.querySelector(".beta-ticker__lane")).toBeNull();
    expect(region.querySelector(".beta-ticker__static")).toHaveTextContent("Private beta");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
