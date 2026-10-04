import { useCallback, useEffect, useRef, useState } from "react";
import { createScan, getScan, listScans, stopScan } from "../api";
import type { ScanConfig, ScanSummary, ScanView } from "./types";

const POLL_MS = 3000;

/**
 * The scan on screen: on opening the page it shows the newest scan (scans run on the server, so leaving the page
 * or closing the browser doesn't stop one), polls it while it runs, and can start, stop or switch scans.
 */
export function useScan() {
  const [scan, setScan] = useState<ScanView>();
  const [scans, setScans] = useState<ScanSummary[]>([]);
  const [error, setError] = useState<string>();
  const [starting, setStarting] = useState(false);
  const shownRef = useRef<string>(); // the scan being shown: a slow reply about another one is dropped

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  const show = useCallback(async (id: string) => {
    shownRef.current = id;
    try {
      const v = await getScan(id);
      if (shownRef.current === id) setScan(v);
    } catch (e) {
      fail(e);
    }
  }, []);

  const refreshList = useCallback(async () => {
    try {
      const list = await listScans();
      setScans(list);
      return list;
    } catch (e) {
      fail(e);
      return [];
    }
  }, []);

  useEffect(() => {
    refreshList().then((list) => {
      if (list[0] && !shownRef.current) show(list[0].scan_id);
    });
  }, [refreshList, show]);

  // Poll while the shown scan runs
  const runningId = scan?.status === "running" ? scan.scan_id : undefined;
  useEffect(() => {
    if (!runningId) return;
    const t = setInterval(() => show(runningId), POLL_MS);
    return () => clearInterval(t);
  }, [runningId, show]);

  // A scan that just finished or stopped: the list's status follows
  const settled = scan && scan.status !== "running" ? scan.scan_id : undefined;
  useEffect(() => {
    if (settled) refreshList();
  }, [settled, refreshList]);

  const start = useCallback(
    async (cfg: ScanConfig) => {
      setStarting(true);
      setError(undefined);
      try {
        const { scan_id } = await createScan(cfg);
        await show(scan_id);
        refreshList();
      } catch (e) {
        fail(e);
      } finally {
        setStarting(false);
      }
    },
    [show, refreshList]
  );

  const stop = useCallback(async () => {
    if (!scan) return;
    try {
      setScan(await stopScan(scan.scan_id));
      refreshList();
    } catch (e) {
      fail(e);
    }
  }, [scan, refreshList]);

  return { scan, scans, error, starting, start, stop, show };
}
