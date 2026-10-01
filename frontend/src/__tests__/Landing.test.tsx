import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Home } from "@/App";
import Landing from "@/pages/Landing";
import { Faq } from "@/components/Landing/Faq";
import { CopyButton } from "@/components/Common/CopyButton";
import { useAuthStore } from "@/store/authStore";

vi.mock("@/api/endpoints", () => ({
  publicAPI: { meta: vi.fn() },
}));
vi.mock("@/api/client", async () => ({ csrfToken: vi.fn(() => null), errorMessage: (e: unknown) => String(e), bootstrapSession: vi.fn() }));
import { publicAPI } from "@/api/endpoints";
import { csrfToken } from "@/api/client";

const META = { coins: ["BTC", "ETH", "USDC", "SOL", "XRP", "XLM"], exchanges: ["bitget"], support_email: "support@minastech.ca", server_ip: "203.0.113.7" };

function mountHome(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/dashboard" element={<p>DASHBOARD</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("/ routing", () => {
  beforeEach(() => {
    vi.mocked(publicAPI.meta).mockResolvedValue(META as never);
    vi.mocked(csrfToken).mockReturnValue(null);
    useAuthStore.setState({ accessToken: null, status: "unknown" } as never);
  });

  it("shows the landing page at once to a visitor with no session cookie, even while the bootstrap is pending", async () => {
    mountHome();
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Buy crypto on a schedule, on your own Bitget account.");
    expect(screen.queryByText("DASHBOARD")).toBeNull();
  });

  it("waits (no landing flash) for a returning session, then goes to the dashboard once signed in", async () => {
    vi.mocked(csrfToken).mockReturnValue("csrf-cookie-present");
    const { rerender } = mountHome();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    useAuthStore.setState({ accessToken: "t", status: "authenticated" } as never);
    rerender(<MemoryRouter initialEntries={["/"]}><Routes><Route path="/" element={<Home />} /><Route path="/dashboard" element={<p>DASHBOARD</p>} /></Routes></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("DASHBOARD")).toBeInTheDocument());
  });

  it("sends a signed-in visitor straight to the dashboard", async () => {
    useAuthStore.setState({ accessToken: "t", status: "authenticated" } as never);
    mountHome();
    await waitFor(() => expect(screen.getByText("DASHBOARD")).toBeInTheDocument());
  });
});

describe("Landing page", () => {
  beforeEach(() => { vi.mocked(publicAPI.meta).mockResolvedValue(META as never); });

  it("reads coins, support address and the server IP from /api/public/meta, with the beta framing and no invented claims", async () => {
    render(<MemoryRouter><Landing /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId("server-ip")).toHaveTextContent("203.0.113.7"));
    expect(screen.getByTestId("beta-badge")).toHaveTextContent("Private beta · by invitation");
    expect(screen.getByTestId("beta-ticker")).toBeInTheDocument();
    expect(screen.getByTestId("coin-list").textContent).toContain("XLM");
    expect(screen.getAllByRole("link", { name: "Request access" })[0]).toHaveAttribute("href", "mailto:support@minastech.ca?subject=AutoBuy%20access%20request");
    expect(screen.getAllByRole("link", { name: "Log in" })[0]).toHaveAttribute("href", "/login");
    const orEmail = screen.getAllByTestId("or-email");
    expect(orEmail).toHaveLength(3);   // header, hero, final call to action
    for (const el of orEmail) { expect(el).toHaveTextContent("or email support@minastech.ca"); expect(el.querySelector("a")).toHaveAttribute("href", "mailto:support@minastech.ca"); }
    expect(screen.getByTestId("flow-wide")).toBeInTheDocument();
    expect(screen.getByTestId("flow-tall")).toBeInTheDocument();
    expect(screen.getByText(/three missed cycles in a row/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Profit Guard|Equity Guard|CoinTech/);
    expect(screen.getByText(/one-time, daily, weekly or monthly/)).toBeInTheDocument();
    expect(screen.getByText(/won't reflect coins you move or add yourself/)).toBeInTheDocument();
    expect(screen.getByText(/not financial advice\. Private beta\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Terms" })).toHaveAttribute("href", "/terms");
    expect(screen.getByText(`© ${new Date().getFullYear()} Minas Academy`)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/testimonial|users trust|% return|certified|SOC ?2|ISO 27001/i);
    expect(screen.getByTestId("flow-animation")).toBeInTheDocument();
  });

  it("falls back to the built-in coin list and support address when meta is unavailable, and shows no IP", async () => {
    vi.mocked(publicAPI.meta).mockRejectedValue(new Error("503"));
    render(<MemoryRouter><Landing /></MemoryRouter>);
    await waitFor(() => expect(screen.getByTestId("server-ip-missing")).toBeInTheDocument());
    expect(screen.getByTestId("coin-list").textContent).toContain("BTC");
    expect(screen.getAllByRole("link", { name: "Request access" })[0].getAttribute("href")).toContain("support@minastech.ca");
  });
});

describe("legal placeholders", () => {
  it("read as a finished beta notice, with no TODO visible to visitors", async () => {
    const { default: Terms } = await import("@/pages/Terms");
    const { default: Privacy } = await import("@/pages/Privacy");
    for (const Page of [Terms, Privacy]) {
      const { unmount } = render(<MemoryRouter><Page /></MemoryRouter>);
      expect(screen.getByTestId("legal-todo")).toHaveTextContent(/being finalised for the private beta\. Email support with any question\./);
      expect(document.body.textContent).not.toMatch(/TODO/);
      unmount();
    }
  });
});

describe("FAQ accordion", () => {
  it("is keyboard operable with aria-expanded and hidden panels", () => {
    render(<Faq items={[{ q: "Q1?", a: "A1" }, { q: "Q2?", a: "A2" }]} />);
    const b1 = screen.getByRole("button", { name: "Q1?" });
    const b2 = screen.getByRole("button", { name: "Q2?" });
    expect(b1).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("A1")).not.toBeVisible();
    b1.focus();
    fireEvent.click(b1);   // native button: Enter/Space fire click
    expect(b1).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("A1")).toBeVisible();
    expect(document.getElementById(b1.getAttribute("aria-controls")!)).toHaveTextContent("A1");
    fireEvent.click(b2);
    expect(b1).toHaveAttribute("aria-expanded", "false");
    expect(b2).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("A1")).not.toBeVisible();
  });
});

describe("CopyButton", () => {
  it("copies the value and confirms, then reverts", async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CopyButton value="203.0.113.7" label="Copy IP" />);
    const btn = screen.getByRole("button", { name: "Copy IP 203.0.113.7" });
    fireEvent.click(btn);
    await vi.advanceTimersByTimeAsync(0);
    expect(writeText).toHaveBeenCalledWith("203.0.113.7");
    expect(btn).toHaveTextContent("Copied");
    await vi.advanceTimersByTimeAsync(2100);
    expect(btn).toHaveTextContent("Copy IP");
    vi.useRealTimers();
  });
});
