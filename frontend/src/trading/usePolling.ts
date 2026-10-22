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
  const latest = useRef(0);

  const reload = useCallback(async () => {
    // Only the newest request's answer counts: a slower one asked before it (the page before a click on page 2, say)
    // must not overwrite it when it finally arrives
    const mine = ++latest.current;
    try {
      const result = await fetcherRef.current();
      if (mine !== latest.current) return;
      setData(result);
      setError(null);
    } catch (e) {
      if (mine !== latest.current) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === latest.current) setLoading(false);
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
