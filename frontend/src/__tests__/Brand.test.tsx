import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Navbar } from "@/components/Layout/Navbar";
import { AuthCard } from "@/components/Auth/AuthCard";

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { email: "me@example.com", kyc_status: "verified" }, logout: vi.fn() }) }));

describe("brand mark", () => {
  it("header shows the symbol (decorative) beside the AutoBuy wordmark", () => {
    render(<MemoryRouter><Navbar onToggleSidebar={() => undefined} /></MemoryRouter>);
    const mark = screen.getByTestId("brand-mark");
    expect(mark).toHaveAttribute("src", "/icon-64.png");
    expect(mark).toHaveAttribute("alt", "");
    expect(mark.parentElement).toHaveTextContent("AutoBuy");
    expect(mark.parentElement).toHaveClass("text-brand-ink");
    expect(screen.getByRole("link", { name: "AutoBuy" })).toHaveAttribute("href", "/dashboard");
  });

  it("login card shows the same symbol and wordmark, never the old A mark", () => {
    render(<AuthCard title="Sign in"><p>form</p></AuthCard>);
    const mark = screen.getByTestId("brand-mark");
    expect(mark).toHaveAttribute("src", "/icon-64.png");
    expect(document.querySelector('img[src="/favicon.svg"]')).toBeNull();
    expect(mark.parentElement).toHaveTextContent("AutoBuy");
  });
});
