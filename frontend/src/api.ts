// Thin wrappers around the backtest-service HTTP + WebSocket API.
// Same-origin: nginx (docker) or the Vite dev proxy forwards these to backtest-service.

import type { RunConfig, RunEvent } from "./types";

export async function createRun(cfg: RunConfig): Promise<{ run_id: string }> {
  const res = await fetch("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export function openRunSocket(runId: string, onEvent: (ev: RunEvent) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${runId}`);
  ws.onmessage = (e) => onEvent(JSON.parse(e.data) as RunEvent);
  return ws;
}
