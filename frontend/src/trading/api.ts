// Typed client for trading-service. nginx (docker) or the Vite dev proxy forwards /api/trading/* to
// trading-service with the prefix stripped.

import type { DipBotCreate, DipBotUpdate, DipPreset, DipPresetConfig, DipSignal, DipStudy, DipTest, DipTestDetail } from "../dip/types";
import { request as http } from "../http";
import type {
  Bot, BotCreate, BotUpdate, Decision, EmailAlert, EmailAlerts, EmailAlertsSave, Order, RotationBotCreate, RotationBotUpdate, Snapshot,
  TradingEvent, TradingStatus,
} from "./types";

const BASE = "/api/trading";

const request = <T>(method: string, path: string, body?: unknown) => http<T>(method, `${BASE}${path}`, body);

export const tradingApi = {
  status: () => request<TradingStatus>("GET", "/status"),
  listBots: (includeArchived = false) => request<Bot[]>("GET", `/bots?include_archived=${includeArchived}`),
  getBot: (id: number) => request<Bot>("GET", `/bots/${id}`),
  createBot: (body: BotCreate) => request<Bot>("POST", "/bots", body),
  createRotationBot: (body: RotationBotCreate) => request<Bot>("POST", "/bots/rotation", body),
  updateBot: (id: number, body: BotUpdate | RotationBotUpdate) => request<Bot>("PATCH", `/bots/${id}`, body),
  pause: (id: number) => request<Bot>("POST", `/bots/${id}/pause`),
  resume: (id: number) => request<Bot>("POST", `/bots/${id}/resume`),
  archive: (id: number) => request<Bot>("POST", `/bots/${id}/archive`),
  runNow: (id: number) => request<Decision>("POST", `/bots/${id}/run`),
  closePosition: (id: number) => request<Order | Order[]>("POST", `/bots/${id}/close`), // a rotation bot: one order per holding
  halt: () => request<{ paused: number }>("POST", "/halt"),
  decisions: (id: number) => request<Decision[]>("GET", `/bots/${id}/decisions`),
  orders: (id: number) => request<Order[]>("GET", `/bots/${id}/orders`),
  events: (id: number) => request<TradingEvent[]>("GET", `/bots/${id}/events`),
  equity: (id: number) => request<Snapshot[]>("GET", `/bots/${id}/equity`),
  allEvents: () => request<TradingEvent[]>("GET", "/events"),
  // Dip buyers (trading-service dip.py): rules, the watchlist (changes while it runs), the blacklist, signals
  createDipBot: (body: DipBotCreate) => request<Bot>("POST", "/bots/dip", body),
  updateDip: (id: number, body: DipBotUpdate) => request<Bot>("PATCH", `/bots/${id}/dip`, body),
  addSymbols: (id: number, symbols: string[]) => request<Bot>("POST", `/bots/${id}/watchlist`, { symbols }),
  removeSymbol: (id: number, symbol: string) => request<Bot>("DELETE", `/bots/${id}/watchlist/${encodeURIComponent(symbol)}`),
  enableSymbol: (id: number, symbol: string) => request<Bot>("POST", `/bots/${id}/watchlist/${encodeURIComponent(symbol)}/enable`),
  blacklistSymbol: (id: number, symbol: string) => request<Bot>("POST", `/bots/${id}/watchlist/${encodeURIComponent(symbol)}/blacklist`),
  sellHolding: (id: number, symbol: string) => request<Order>("POST", `/bots/${id}/holdings/${encodeURIComponent(symbol)}/sell`),
  botSignals: (id: number) => request<DipSignal[]>("GET", `/bots/${id}/signals`),
  /** Every dip bot's signals, newest first; afterId = only newer ones (for notifications). */
  signals: (afterId = 0, limit = 100) => request<DipSignal[]>("GET", `/signals?after_id=${afterId}&limit=${limit}`),
  // Saved dip buyer setups: saving under a name that exists (ignoring case) replaces it
  dipPresets: () => request<DipPreset[]>("GET", "/dip/presets"),
  saveDipPreset: (name: string, config: DipPresetConfig) => request<DipPreset>("POST", "/dip/presets", { name, config }),
  deleteDipPreset: (id: number) => request<void>("DELETE", `/dip/presets/${id}`),
  // Dip buyer test batches (parameter sweeps) and each setup they tested
  dipStudies: () => request<DipStudy[]>("GET", "/dip/studies"),
  dipTests: (studyId: number) => request<DipTest[]>("GET", `/dip/studies/${studyId}/tests`),
  dipTest: (id: number) => request<DipTestDetail>("GET", `/dip/tests/${id}`),
  // Email alerts: who gets them and about what; the mail server itself is set in trading-service/.env
  emailAlerts: () => request<EmailAlerts>("GET", "/email-alerts"),
  saveEmailAlerts: (body: EmailAlertsSave) => request<EmailAlerts>("PUT", "/email-alerts", body),
  testEmail: () => request<EmailAlert>("POST", "/email-alerts/test"),
  emailHistory: (limit = 50) => request<EmailAlert[]>("GET", `/email-alerts/history?limit=${limit}`),
};
