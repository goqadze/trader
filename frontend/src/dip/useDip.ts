import { useCallback, useEffect, useRef, useState } from "react";
import { createDipRun, getRun } from "../api";
import type { DipConfig, DipRun } from "./types";

const POLL_MS = 2000;
const STORE_KEY = "dip:last"; // the last practice/exam run ids, so coming back to the page shows them again

export type Period = "practice" | "exam";
type Runs = Partial<Record<Period, DipRun>>;

function remember(ids: Partial<Record<Period, string>> | null) {
  try {
    if (ids) localStorage.setItem(STORE_KEY, JSON.stringify(ids));
    else localStorage.removeItem(STORE_KEY);
  } catch {
    /* private window or blocked storage: the page still works, it just won't remember */
  }
}

function remembered(): Partial<Record<Period, string>> | null {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "null");
  } catch {
    return null;
  }
}

/** A dip-buyer backtest's practice and exam runs: started together, polled until both finish (like useRotation). */
export function useDip() {
  const [runs, setRuns] = useState<Runs>({});
  const [error, setError] = useState<string>();
  const [starting, setStarting] = useState(false);
  const idsRef = useRef<Partial<Record<Period, string>>>({});

  const fetchAll = useCallback(async () => {
    const entries = await Promise.all(
      (Object.entries(idsRef.current) as [Period, string][]).map(async ([p, id]) => {
        try {
          return [p, (await getRun(id)) as unknown as DipRun] as const;
        } catch {
          return [p, undefined] as const; // forgotten (backtest-service restarted)
        }
      })
    );
    setRuns(Object.fromEntries(entries.filter(([, r]) => r)) as Runs);
    if (entries.length && entries.every(([, r]) => !r)) remember(null);
  }, []);

  useEffect(() => {
    const ids = remembered();
    if (ids) {
      idsRef.current = ids;
      void fetchAll();
    }
  }, [fetchAll]);

  const running = Object.values(runs).some((r) => r && (r.status === "pending" || r.status === "running"));
  useEffect(() => {
    if (!running) return;
    const t = setInterval(fetchAll, POLL_MS);
    return () => clearInterval(t);
  }, [running, fetchAll]);

  const start = useCallback(
    async (configs: Partial<Record<Period, DipConfig>>) => {
      setStarting(true);
      setError(undefined);
      try {
        const ids: Partial<Record<Period, string>> = {};
        for (const [p, cfg] of Object.entries(configs) as [Period, DipConfig][]) ids[p] = (await createDipRun(cfg)).run_id;
        idsRef.current = ids;
        remember(ids);
        await fetchAll();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setStarting(false);
      }
    },
    [fetchAll]
  );

  return { runs, error, starting, running, start };
}
