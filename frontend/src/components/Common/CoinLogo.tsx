import { useState } from "react";
import { assetColor } from "@/utils/assetColors";
import { logoEntry } from "@/utils/logoSources";

/** A coin's mark at `size` px: the logo from /logos/ on a white plate when
 *  LOGO_SOURCES.md lists one, otherwise (or when the file fails to load) a
 *  circle in the asset colour with the coin's first letter. Decorative: the
 *  symbol and name are always rendered next to it, so alt is empty. */
export function CoinLogo({ symbol, size = 32, className = "" }: { symbol: string; size?: number; className?: string }) {
  const [failed, setFailed] = useState(false);
  const entry = logoEntry(symbol);
  const box = { width: size, height: size };
  if (!entry?.file || failed) {
    return (
      // the initial is drawn by CSS from data-letter, so the surrounding text content stays the plain symbol
      <span aria-hidden className={`inline-flex shrink-0 select-none items-center justify-center rounded-full font-extrabold text-white before:content-[attr(data-letter)] ${className}`}
            style={{ ...box, background: assetColor(symbol), fontSize: Math.round(size * 0.5), lineHeight: 1 }}
            data-testid="coin-letter" data-symbol={symbol} data-letter={symbol.charAt(0).toUpperCase()} />
    );
  }
  const inner = entry.shape === "mark" ? Math.round(size * 0.68) : size;
  return (
    <span aria-hidden className={`inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-white ring-1 ring-slate-200 ${className}`}
          style={box} data-testid="coin-logo" data-symbol={symbol}>
      <img src={`/logos/${entry.file}`} alt="" width={inner} height={inner} style={{ width: inner, height: inner }} draggable={false}
           onError={() => setFailed(true)} />
    </span>
  );
}
