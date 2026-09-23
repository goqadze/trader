import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Fetch data now and then every `intervalMs`, pausing while the browser tab is hidden
 * (no point hammering the API for a page nobody is looking at). Bots decide once a day and
 * check stops every few minutes, so polling every ~15s is plenty -- no WebSocket needed.
 *
 * Returns the latest data, the last error, and `reload()` to refresh right after a user action.
 */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs = 15_000, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(async () => {
    try {
      const result = await fetcherRef.current();
      setData(result);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    reload();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, intervalMs);
    const onVisible = () => document.visibilityState === "visible" && reload();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, reload, ...deps]);

  return { data, error, loading, reload };
}
