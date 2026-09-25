import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "@/components/Common/StatusBadge";
import { ASSET_COLORS } from "@/utils/assetColors";
import tailwind from "../../tailwind.config.js";

const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");   // vitest runs from frontend/
const meta = (attr: string, name: string) => html.match(new RegExp(`<meta ${attr}="${name}" content="([^"]*)"`))?.[1];

describe("brand palette", () => {
  it("brand-600 is the sampled deep purple and the scale is monotonic", () => {
    const brand = (tailwind as { theme: { extend: { colors: { brand: Record<number, string> } } } }).theme.extend.colors.brand;
    expect(brand[600]).toBe("#543477");
    const lum = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).reduce((a, b) => a + b);
    const keys = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900];
    for (let i = 1; i < keys.length; i++) expect(lum(brand[keys[i]])).toBeLessThan(lum(brand[keys[i - 1]]));
  });

  it("asset colours stay distinct from each other and none is the brand purple", () => {
    const values = Object.values(ASSET_COLORS).map((c) => c.toLowerCase());
    expect(new Set(values).size).toBe(values.length);
    expect(values).not.toContain("#543477");
    expect(ASSET_COLORS.SOL).toBe("#db2777");
  });

  it("status colours keep their semantics; the accumulate chip uses the tan accent", () => {
    const cls = (s: string) => { const { container } = render(<StatusBadge status={s} />); return container.firstElementChild!.className; };
    expect(cls("verified")).toContain("bg-green-100");
    expect(cls("pending")).toContain("bg-amber-100");
    expect(cls("failed")).toContain("bg-red-100");
    expect(cls("active")).toContain("bg-brand-100");
    expect(cls("accumulate")).toContain("bg-tan-100");
    expect(screen.getAllByText(/verified|pending|failed|active|accumulate/).length).toBeGreaterThan(0);
  });
});

describe("link preview meta tags", () => {
  it("carry absolute production URLs, the image sizes, a plain description and no localhost", () => {
    expect(html).toMatch(/<title>AutoBuy<\/title>/);
    expect(meta("property", "og:type")).toBe("website");
    expect(meta("property", "og:site_name")).toBe("AutoBuy");
    expect(meta("property", "og:title")).toBe("AutoBuy");
    expect(meta("property", "og:url")).toBe("https://autobuy.minastrade.com/");
    expect(meta("property", "og:image")).toBe("https://autobuy.minastrade.com/og-image.png");
    expect(meta("property", "og:image:width")).toBe("1200");
    expect(meta("property", "og:image:height")).toBe("630");
    expect(meta("property", "og:image:alt")).toMatch(/AutoBuy logo/);
    expect(meta("name", "twitter:card")).toBe("summary_large_image");
    expect(meta("name", "twitter:image")).toBe("https://autobuy.minastrade.com/og-image.png");
    expect(meta("name", "twitter:title")).toBe("AutoBuy");
    const description = meta("name", "description")!;
    expect(description).toBe(meta("property", "og:description"));
    expect(description).toBe(meta("name", "twitter:description"));
    expect(description).toMatch(/own wallet/);
    expect(description).not.toMatch(/wealth|grow|profit|return|invest/i);
    expect(html).not.toMatch(/localhost|127\.0\.0\.1|vercel\.app|ngrok|:5173|:8080/);
  });
});
