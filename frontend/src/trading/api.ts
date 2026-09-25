// Typed client for trading-service. nginx (docker) or the Vite dev proxy forwards /api/trading/* to
// trading-service with the prefix stripped.

import { request as http } from "../http";
import type { Bot, BotCreate, BotUpdate, Decision, Order, Snapshot, TradingEvent, TradingStatus } from "./types";

export { ApiError } from "../http";

const BASE = "/api/trading";

const request = <T>(method: string, path: string, body?: unknown) => http<T>(method, `${BASE}${path}`, body);

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
