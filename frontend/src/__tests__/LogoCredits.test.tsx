import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import LogoCredits from "@/pages/LogoCredits";
import { LOGO_ENTRIES } from "@/utils/logoSources";

describe("Logo credits page", () => {
  it("lists only the coins that display a logo, with source and licence, the ETH attribution, and one line for the rest", () => {
    render(<MemoryRouter><LogoCredits /></MemoryRouter>);
    for (const e of LOGO_ENTRIES.filter((e) => !e.file)) expect(screen.queryByTestId(`credit-${e.symbol}`)).toBeNull();
    expect(screen.getByTestId("pending-logos")).toHaveTextContent("XLM, SHIB, PEPE and BONK are shown with their initial until an official logo is available.");
    for (const e of LOGO_ENTRIES.filter((e) => e.file)) {
      const row = screen.getByTestId(`credit-${e.symbol}`);
      expect(row).toHaveTextContent(e.symbol);
      expect(row).toHaveTextContent(e.source.slice(0, 20));
      if (e.notice) expect(screen.getByTestId(`notice-${e.symbol}`)).toHaveTextContent(e.notice);
    }
    expect(screen.getByTestId("notice-ETH")).toHaveTextContent("CC BY 4.0");
    expect(screen.queryByTestId("notice-XLM")).toBeNull();   // Stellar's notice appears once its row carries one
    for (const a of screen.getAllByRole("link", { name: /./ })) {
      const href = a.getAttribute("href") ?? "";
      if (href.startsWith("http")) expect(a).toHaveAttribute("rel", "noopener noreferrer");
    }
  });
});
