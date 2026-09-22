import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "@/api/client";

/** Run an async loader on mount (and on `reload()`), tracking loading/error. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await loader());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  /** Re-run the loader without the loading state: the current data stays on
   *  screen and is replaced when the new result arrives; a failure is ignored. */
  const refresh = useCallback(async () => {
    try { setData(await loader()); } catch { /* keep what is shown */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => { void reload(); }, [reload]);
  return { data, loading, error, reload, refresh, setData };
}
