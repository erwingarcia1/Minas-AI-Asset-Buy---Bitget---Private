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
  return (
    <div ref={ref} className={`flow${playing ? " is-playing" : ""}${reduced ? " is-static" : ""}`} data-testid="flow-animation" data-playing={playing ? "1" : "0"}>
      <svg viewBox="0 0 640 190" role="img" aria-labelledby="flow-title flow-desc" className="flow__svg">
        <title id="flow-title">How AutoBuy works</title>
        <desc id="flow-desc">USDT sits in your Bitget sub-account. On schedule, AutoBuy places a buy order on Bitget. The coin lands in the same sub-account.</desc>
        <defs>
          <marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="#b0a2c0" />
          </marker>
        </defs>
        {/* rails */}
        <line x1="112" y1="80" x2="196" y2="80" className="flow__rail" markerEnd="url(#flow-arrow)" />
        <line x1="276" y1="80" x2="360" y2="80" className="flow__rail" markerEnd="url(#flow-arrow)" />
        <line x1="440" y1="80" x2="524" y2="80" className="flow__rail" markerEnd="url(#flow-arrow)" />
        {/* the travelling dot */}
        <circle className="flow__dot" cy="80" r="6" />

        {/* 1. USDT in the sub-account */}
        <g className="flow__node">
          <rect x="24" y="36" width="88" height="88" rx="16" className="flow__box" />
          <circle cx="68" cy="80" r="20" className="flow__coin flow__coin--usdt" />
          <text x="68" y="85" textAnchor="middle" className="flow__coin-label">USDT</text>
          <text x="68" y="150" textAnchor="middle" className="flow__label">Your sub-account</text>
          <text x="68" y="168" textAnchor="middle" className="flow__sublabel">USDT ready</text>
        </g>
        {/* 2. AutoBuy schedule */}
        <g className="flow__node">
          <rect x="196" y="36" width="88" height="88" rx="16" className="flow__box flow__box--brand" />
          <circle cx="240" cy="80" r="22" className="flow__clock" />
          <line x1="240" y1="80" x2="240" y2="66" className="flow__hand flow__hand--hour" />
          <line x1="240" y1="80" x2="252" y2="80" className="flow__hand flow__hand--minute" />
          <text x="240" y="150" textAnchor="middle" className="flow__label">AutoBuy</text>
          <text x="240" y="168" textAnchor="middle" className="flow__sublabel">on schedule</text>
        </g>
        {/* 3. Order on Bitget */}
        <g className="flow__node">
          <rect x="360" y="36" width="88" height="88" rx="16" className="flow__box" />
          <rect x="384" y="62" width="40" height="36" rx="4" className="flow__ticket" />
          <line x1="392" y1="72" x2="416" y2="72" className="flow__ticket-line" />
          <line x1="392" y1="80" x2="412" y2="80" className="flow__ticket-line" />
          <line x1="392" y1="88" x2="406" y2="88" className="flow__ticket-line" />
          <path d="M416 92l-6-6 2.5-2.5 3.5 3.5 7-7 2.5 2.5z" className="flow__tick" />
          <text x="404" y="150" textAnchor="middle" className="flow__label">Bitget</text>
          <text x="404" y="168" textAnchor="middle" className="flow__sublabel">buy order placed</text>
        </g>
        {/* 4. Coin in the same sub-account */}
        <g className="flow__node">
          <rect x="524" y="36" width="88" height="88" rx="16" className="flow__box" />
          <circle cx="568" cy="80" r="20" className="flow__coin flow__coin--btc" />
          <text x="568" y="85" textAnchor="middle" className="flow__coin-label">BTC</text>
          <text x="568" y="150" textAnchor="middle" className="flow__label">Same sub-account</text>
          <text x="568" y="168" textAnchor="middle" className="flow__sublabel">coin received</text>
        </g>
      </svg>
    </div>
  );
}
