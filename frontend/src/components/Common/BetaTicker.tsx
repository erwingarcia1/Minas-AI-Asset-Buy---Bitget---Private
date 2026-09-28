import { useEffect, useState } from "react";
import { BETA_TICKER_ENABLED, BETA_TICKER_TEXT } from "@/config/beta";

/** Thin private-beta bar shown above every page while BETA_TICKER_ENABLED.
 *  The text scrolls as a seamless loop (CSS transform only), pauses on hover
 *  and focus and via a visible pause/play button (WCAG 2.2.2), is static
 *  under prefers-reduced-motion, and is read once by screen readers (the
 *  moving copies are aria-hidden). Not dismissible. */
export function BetaTicker() {
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return;
    const apply = () => setReduced(mq.matches);
    apply();
    mq.addEventListener?.("change", apply);
    return () => mq.removeEventListener?.("change", apply);
  }, []);
  if (!BETA_TICKER_ENABLED) return null;
  const moving = !reduced && !paused;
  return (
    <div className="beta-ticker" role="region" aria-label="Private beta notice" data-testid="beta-ticker" data-moving={moving ? "1" : "0"}>
      <p className="sr-only">{BETA_TICKER_TEXT}</p>
      <div className="beta-ticker__track" aria-hidden="true">
        {reduced ? (
          <span className="beta-ticker__static">{BETA_TICKER_TEXT}</span>
        ) : (
          <div className={`beta-ticker__lane${paused ? " is-paused" : ""}`}>
            <span className="beta-ticker__item">{BETA_TICKER_TEXT}</span>
            <span className="beta-ticker__item">{BETA_TICKER_TEXT}</span>
          </div>
        )}
      </div>
      {!reduced && (
        <button type="button" className="beta-ticker__btn" aria-pressed={paused} aria-label={paused ? "Play notice" : "Pause notice"}
                onClick={() => setPaused((v) => !v)}>
          {paused ? "\u25B6" : "\u275A\u275A"}
        </button>
      )}
    </div>
  );
}
