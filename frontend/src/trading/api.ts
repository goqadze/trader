// Typed client for trading-service. Same-origin: nginx (docker) or the Vite dev proxy forwards
// /api/trading/* to trading-service with the prefix stripped.

import type { Bot, BotCreate, BotUpdate, Decision, Order, Snapshot, TradingEvent, TradingStatus } from "./types";

const BASE = "/api/trading";

/** Error carrying the server's explanation (FastAPI puts it in `detail`). */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      // 422 validation errors come as a list of {loc, msg}; everything else as a string
      msg = Array.isArray(data.detail)
        ? data.detail.map((d: { loc: string[]; msg: string }) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join("; ")
        : data.detail ?? msg;
    } catch {
      /* non-JSON error body: keep the status text */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const tradingApi = {
  status: () => request<TradingStatus>("GET", "/status"),
  listBots: (includeArchived = false) => request<Bot[]>("GET", `/bots?include_archived=${includeArchived}`),
  getBot: (id: number) => request<Bot>("GET", `/bots/${id}`),
  createBot: (body: BotCreate) => request<Bot>("POST", "/bots", body),
  updateBot: (id: number, body: BotUpdate) => request<Bot>("PATCH", `/bots/${id}`, body),
  pause: (id: number) => request<Bot>("POST", `/bots/${id}/pause`),
  resume: (id: number) => request<Bot>("POST", `/bots/${id}/resume`),
  archive: (id: number) => request<Bot>("POST", `/bots/${id}/archive`),
  runNow: (id: number) => request<Decision>("POST", `/bots/${id}/run`),
  closePosition: (id: number) => request<Order>("POST", `/bots/${id}/close`),
  halt: () => request<{ paused: number }>("POST", "/halt"),
  decisions: (id: number) => request<Decision[]>("GET", `/bots/${id}/decisions`),
  orders: (id: number) => request<Order[]>("GET", `/bots/${id}/orders`),
  events: (id: number) => request<TradingEvent[]>("GET", `/bots/${id}/events`),
  equity: (id: number) => request<Snapshot[]>("GET", `/bots/${id}/equity`),
  allEvents: () => request<TradingEvent[]>("GET", "/events"),
};
