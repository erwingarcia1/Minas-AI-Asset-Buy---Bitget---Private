import { useEffect, useRef, useState } from "react";

/** The one landing-page animation: a looping flow USDT in the sub-account →
 *  AutoBuy schedule → order placed on Bitget → coin in the same sub-account.
 *  Inline SVG with CSS keyframes (no dependency), a fixed aspect ratio so it
 *  never shifts layout, paused while offscreen (IntersectionObserver), and a
 *  static frame under prefers-reduced-motion. No prices, no rising numbers. */
export function FlowAnimation() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(true);
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return undefined;
    const apply = () => setReduced(mq.matches);
    apply();
    mq.addEventListener?.("change", apply);
    return () => mq.removeEventListener?.("change", apply);
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { threshold: 0.1 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  const playing = visible && !reduced;
  const cls = `flow${playing ? " is-playing" : ""}${reduced ? " is-static" : ""}`;
  return (
    <div ref={ref} className={cls} data-testid="flow-animation" data-playing={playing ? "1" : "0"}>
      {/* wide: left to right (from 640px) */}
      <svg viewBox="0 0 640 190" role="img" aria-labelledby="flow-title flow-desc" className="flow__svg flow__svg--wide" data-testid="flow-wide">
        <title id="flow-title">How AutoBuy works</title>
        <desc id="flow-desc">USDT sits in your Bitget sub-account. On schedule, AutoBuy places a buy order on Bitget. The coin lands in the same sub-account.</desc>
        <defs>
          <marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="#b0a2c0" />
          </marker>
        </defs>
        <line x1="112" y1="80" x2="196" y2="80" className="flow__rail" markerEnd="url(#flow-arrow)" />
        <line x1="276" y1="80" x2="360" y2="80" className="flow__rail" markerEnd="url(#flow-arrow)" />
        <line x1="440" y1="80" x2="524" y2="80" className="flow__rail" markerEnd="url(#flow-arrow)" />
        <circle className="flow__dot flow__dot--h" cy="80" r="6" />
        {nodes("h")}
      </svg>
      {/* narrow: top to bottom (below 640px) */}
      <svg viewBox="0 0 320 560" role="img" aria-labelledby="flow-title-v flow-desc-v" className="flow__svg flow__svg--tall" data-testid="flow-tall">
        <title id="flow-title-v">How AutoBuy works</title>
        <desc id="flow-desc-v">USDT sits in your Bitget sub-account. On schedule, AutoBuy places a buy order on Bitget. The coin lands in the same sub-account.</desc>
        <defs>
          <marker id="flow-arrow-v" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="#b0a2c0" />
          </marker>
        </defs>
        <line x1="60" y1="108" x2="60" y2="150" className="flow__rail" markerEnd="url(#flow-arrow-v)" />
        <line x1="60" y1="258" x2="60" y2="300" className="flow__rail" markerEnd="url(#flow-arrow-v)" />
        <line x1="60" y1="408" x2="60" y2="450" className="flow__rail" markerEnd="url(#flow-arrow-v)" />
        <circle className="flow__dot flow__dot--v" cx="60" r="6" />
        {nodes("v")}
      </svg>
    </div>
  );
}

/** The four nodes, laid out horizontally ("h", labels under the box) or vertically ("v", labels to the right). */
function nodes(layout: "h" | "v") {
  const spots = layout === "h"
    ? [{ x: 24, y: 36, lx: 68, ly: 150, anchor: "middle" as const }, { x: 196, y: 36, lx: 240, ly: 150, anchor: "middle" as const },
       { x: 360, y: 36, lx: 404, ly: 150, anchor: "middle" as const }, { x: 524, y: 36, lx: 568, ly: 150, anchor: "middle" as const }]
    : [{ x: 16, y: 20, lx: 124, ly: 58, anchor: "start" as const }, { x: 16, y: 170, lx: 124, ly: 208, anchor: "start" as const },
       { x: 16, y: 320, lx: 124, ly: 358, anchor: "start" as const }, { x: 16, y: 470, lx: 124, ly: 508, anchor: "start" as const }];
  const [a, b, c, d] = spots;
  const cx = (s: typeof a) => s.x + 44, cy = (s: typeof a) => s.y + 44;
  return (
    <>
      <g className="flow__node">
        <rect x={a.x} y={a.y} width="88" height="88" rx="16" className="flow__box" />
        <circle cx={cx(a)} cy={cy(a)} r="20" className="flow__coin flow__coin--usdt" />
        <text x={cx(a)} y={cy(a) + 5} textAnchor="middle" className="flow__coin-label">USDT</text>
        <text x={a.lx} y={a.ly} textAnchor={a.anchor} className="flow__label">Your sub-account</text>
        <text x={a.lx} y={a.ly + 18} textAnchor={a.anchor} className="flow__sublabel">USDT ready</text>
      </g>
      <g className="flow__node">
        <rect x={b.x} y={b.y} width="88" height="88" rx="16" className="flow__box flow__box--brand" />
        <circle cx={cx(b)} cy={cy(b)} r="22" className="flow__clock" />
        <line x1={cx(b)} y1={cy(b)} x2={cx(b)} y2={cy(b) - 14} className="flow__hand flow__hand--hour" />
        <line x1={cx(b)} y1={cy(b)} x2={cx(b) + 12} y2={cy(b)} className="flow__hand flow__hand--minute" style={{ transformOrigin: `${cx(b)}px ${cy(b)}px` }} />
        <text x={b.lx} y={b.ly} textAnchor={b.anchor} className="flow__label">AutoBuy</text>
        <text x={b.lx} y={b.ly + 18} textAnchor={b.anchor} className="flow__sublabel">on schedule</text>
      </g>
      <g className="flow__node">
        <rect x={c.x} y={c.y} width="88" height="88" rx="16" className="flow__box" />
        <rect x={cx(c) - 20} y={cy(c) - 18} width="40" height="36" rx="4" className="flow__ticket" />
        <line x1={cx(c) - 12} y1={cy(c) - 8} x2={cx(c) + 12} y2={cy(c) - 8} className="flow__ticket-line" />
        <line x1={cx(c) - 12} y1={cy(c)} x2={cx(c) + 8} y2={cy(c)} className="flow__ticket-line" />
        <line x1={cx(c) - 12} y1={cy(c) + 8} x2={cx(c) + 2} y2={cy(c) + 8} className="flow__ticket-line" />
        <path d={`M${cx(c) + 12} ${cy(c) + 12}l-6-6 2.5-2.5 3.5 3.5 7-7 2.5 2.5z`} className="flow__tick" />
        <text x={c.lx} y={c.ly} textAnchor={c.anchor} className="flow__label">Bitget</text>
        <text x={c.lx} y={c.ly + 18} textAnchor={c.anchor} className="flow__sublabel">buy order placed</text>
      </g>
      <g className="flow__node">
        <rect x={d.x} y={d.y} width="88" height="88" rx="16" className="flow__box" />
        <circle cx={cx(d)} cy={cy(d)} r="20" className="flow__coin flow__coin--btc" />
        <text x={cx(d)} y={cy(d) + 5} textAnchor="middle" className="flow__coin-label">BTC</text>
        <text x={d.lx} y={d.ly} textAnchor={d.anchor} className="flow__label">Same sub-account</text>
        <text x={d.lx} y={d.ly + 18} textAnchor={d.anchor} className="flow__sublabel">coin received</text>
      </g>
    </>
  );
}
