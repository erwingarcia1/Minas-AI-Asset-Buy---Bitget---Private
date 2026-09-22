import { useEffect } from "react";

/** Dashboard refresh policy (no polling loop): the charts refetch when the tab
 *  regains visibility or focus, right after a purchase action anywhere in the
 *  app (notifyPortfolioChanged), and at most once every 5 minutes while the
 *  tab stays visible. Bursts (focus + visibility firing together) are
 *  collapsed by MIN_GAP_MS. */
export const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const MIN_GAP_MS = 2000;

const listeners = new Set<() => void>();

/** Call after a purchase is created, changed or deleted so mounted charts refetch. */
export function notifyPortfolioChanged() {
  listeners.forEach((l) => l());
}

export function usePortfolioRefresh(refresh: () => void) {
  useEffect(() => {
    let last = Date.now();
    const run = () => {
      if (Date.now() - last < MIN_GAP_MS) return;
      last = Date.now();
      refresh();
    };
    const onVisible = () => { if (document.visibilityState === "visible") run(); };
    const onFocus = () => { if (document.visibilityState === "visible") run(); };
    const onChanged = () => { last = 0; run(); };
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") run(); }, REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);
    listeners.add(onChanged);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onFocus);
      listeners.delete(onChanged);
    };
  }, [refresh]);
}
