// Thin wrappers around the backtest-service HTTP + WebSocket API.
// Same-origin: nginx (docker) or the Vite dev proxy forwards these to backtest-service.

import { request } from "./http";
import type { RotationConfig } from "./rotation/types";
import type { ScanConfig, ScanSummary, ScanView } from "./scan/types";
import type { Result, RunConfig, RunEvent } from "./types";

export const createRun = (cfg: RunConfig) => request<{ run_id: string }>("POST", "/runs", cfg);

export function openRunSocket(runId: string, onEvent: (ev: RunEvent) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${runId}`);
  ws.onmessage = (e) => onEvent(JSON.parse(e.data) as RunEvent);
  return ws;
}

// --- Scans: many symbols x strategies on a practice and an exam window (backtest-service/app/scan.py) ---
export const createScan = (cfg: ScanConfig) => request<{ scan_id: string }>("POST", "/scans", cfg);
export const listScans = () => request<ScanSummary[]>("GET", "/scans");
export const getScan = (id: string) => request<ScanView>("GET", `/scans/${id}`);
export const stopScan = (id: string) => request<ScanView>("POST", `/scans/${id}/stop`);

/** One run's full detail (config + result), e.g. a scan combination opened for its checklist. */
export const getRun = (id: string) =>
  request<{ run_id: string; config: RunConfig; status: string; result: Result | null }>("GET", `/runs/${id}`);

/** A momentum-rotation backtest over a universe of symbols (one window); poll getRun for its result. */
export const createRotationRun = (cfg: RotationConfig) => request<{ run_id: string }>("POST", "/runs/rotation", cfg);
