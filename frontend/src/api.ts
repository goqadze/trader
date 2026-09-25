// Thin wrappers around the backtest-service HTTP + WebSocket API.
// Same-origin: nginx (docker) or the Vite dev proxy forwards these to backtest-service.

import { request } from "./http";
import type { RunConfig, RunEvent } from "./types";

export const createRun = (cfg: RunConfig) => request<{ run_id: string }>("POST", "/runs", cfg);

export function openRunSocket(runId: string, onEvent: (ev: RunEvent) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${runId}`);
  ws.onmessage = (e) => onEvent(JSON.parse(e.data) as RunEvent);
  return ws;
}
