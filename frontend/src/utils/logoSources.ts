import raw from "../../public/logos/LOGO_SOURCES.md?raw";

/** One row of public/logos/LOGO_SOURCES.md. `file` empty means the coin shows a letter circle. */
export interface LogoEntry {
  symbol: string;
  file: string;          // name inside /logos/, or "" for a letter circle
  shape: "circle" | "mark" | "";
  source: string;
  sourceUrl: string;
  licence: string;
  notice: string;        // shown to users on the credits page (an attribution the licence asks for)
}

function parseTable(md: string): LogoEntry[] {
  const rows: LogoEntry[] = [];
  for (const line of md.split("\n")) {
    if (!line.startsWith("|")) continue;
    const cells = line.split("|").slice(1, -1).map((c) => c.trim());
    if (cells.length < 7 || cells[0] === "Coin" || /^-+$/.test(cells[0])) continue;
    const shape = cells[2] === "circle" || cells[2] === "mark" ? cells[2] : "";
    rows.push({ symbol: cells[0], file: cells[1], shape, source: cells[3], sourceUrl: cells[4], licence: cells[5], notice: cells[6] });
  }
  return rows;
}

export const LOGO_ENTRIES: LogoEntry[] = parseTable(raw);

export function logoEntry(symbol: string): LogoEntry | undefined {
  return LOGO_ENTRIES.find((e) => e.symbol === symbol.toUpperCase());
}

/** Same-origin path of the coin's logo file, or null when the coin has none. */
export function logoPath(symbol: string): string | null {
  const e = logoEntry(symbol);
  return e && e.file ? `/logos/${e.file}` : null;
}
