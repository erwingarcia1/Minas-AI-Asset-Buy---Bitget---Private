import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import LogoCredits from "@/pages/LogoCredits";
import { LOGO_ENTRIES } from "@/utils/logoSources";

describe("Logo credits page", () => {
  it("lists every coin from LOGO_SOURCES.md with its source and licence, and shows the ETH attribution", () => {
    render(<MemoryRouter><LogoCredits /></MemoryRouter>);
    for (const e of LOGO_ENTRIES) {
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
