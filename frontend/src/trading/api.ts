// Typed client for trading-service. nginx (docker) or the Vite dev proxy forwards /api/trading/* to
// trading-service with the prefix stripped.

import type { DipBotCreate, DipBotUpdate, DipPreset, DipPresetConfig, DipSignal } from "../dip/types";
import { request as http } from "../http";
import type { RotationPresetConfig } from "../rotation/types";
import type { SavedSetup, Study, StudyStrategy, StudySummary, StudyTestDetail, StudyTestPage, TestQuery } from "../studies/types";
import type {
  AccountInfo, Bot, BotCreate, BotTable, BotTableQuery, BotUpdate, CapitalAdd, CapitalWithdraw, Decision, EmailAlert, EmailAlerts, EmailAlertsSave, Order, RotationBotCreate, RotationBotUpdate, Snapshot,
  TradingEvent, TradingStatus, WithdrawalPlan,
} from "./types";

const BASE = "/api/trading";

const request = <T>(method: string, path: string, body?: unknown) => http<T>(method, `${BASE}${path}`, body);

export const tradingApi = {
  status: () => request<TradingStatus>("GET", "/status"),
  accounts: () => request<AccountInfo[]>("GET", "/accounts"),
  listBots: (includeArchived = false) => request<Bot[]>("GET", `/bots?include_archived=${includeArchived}`),
  // The overview's bot table, one page at a time: filtered and sorted by trading-service (main.bot_table)
  botTable: (query: BotTableQuery) => {
    const params = new URLSearchParams({ broker: query.broker, include_archived: String(query.archived), offset: String(query.offset), limit: String(query.limit) });
    if (query.sort) params.set("sort", query.sort);
    params.set("order", query.order);
    for (const k of query.kinds) params.append("kind", k);
    for (const s of query.sectors) params.append("sector", s);
    return request<BotTable>("GET", `/bots/table?${params}`);
  },
  getBot: (id: number) => request<Bot>("GET", `/bots/${id}`),
  createBot: (body: BotCreate) => request<Bot>("POST", "/bots", body),
  createRotationBot: (body: RotationBotCreate) => request<Bot>("POST", "/bots/rotation", body),
  updateBot: (id: number, body: BotUpdate | RotationBotUpdate) => request<Bot>("PATCH", `/bots/${id}`, body),
  pause: (id: number) => request<Bot>("POST", `/bots/${id}/pause`),
  resume: (id: number) => request<Bot>("POST", `/bots/${id}/resume`),
  archive: (id: number) => request<Bot>("POST", `/bots/${id}/archive`),
  addCapital: (id: number, body: CapitalAdd) => request<Bot>("POST", `/bots/${id}/capital`, body),
  withdrawalPlan: (id: number, amount: number) => request<WithdrawalPlan>("GET", `/bots/${id}/withdraw?amount=${amount}`),
  withdraw: (id: number, body: CapitalWithdraw) => request<Bot>("POST", `/bots/${id}/withdraw`, body),
  runNow: (id: number) => request<Decision>("POST", `/bots/${id}/run`),
  closePosition: (id: number) => request<Order | Order[]>("POST", `/bots/${id}/close`), // a rotation bot: one order per holding
  halt: () => request<{ paused: number }>("POST", "/halt"),
  sectors: () => request<string[]>("GET", "/sectors"),
  // null: back to automatic (from its symbols); [] = no badge
  setSectors: (id: number, sectors: string[] | null) => request<Bot>("PUT", `/bots/${id}/sectors`, { sectors }),
  setRating: (id: number, rating: number) => request<Bot>("PUT", `/bots/${id}/rating`, { rating }), // 0 = not rated
  decisions: (id: number) => request<Decision[]>("GET", `/bots/${id}/decisions`),
  orders: (id: number) => request<Order[]>("GET", `/bots/${id}/orders`),
  events: (id: number) => request<TradingEvent[]>("GET", `/bots/${id}/events`),
  equity: (id: number) => request<Snapshot[]>("GET", `/bots/${id}/equity`),
  allEvents: (broker?: string) => request<TradingEvent[]>("GET", broker ? `/events?broker=${encodeURIComponent(broker)}` : "/events"),
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
  // testId: saved from that Test results test, so it keeps a copy of it (its results: a bot started from it shows them)
  saveDipPreset: (name: string, config: DipPresetConfig, testId?: number) =>
    request<DipPreset>("POST", "/dip/presets", { name, config, test_id: testId }),
  deleteDipPreset: (id: number) => request<void>("DELETE", `/dip/presets/${id}`),
  // Saved momentum rotation setups (the same names can't be a dip buyer's)
  rotationPresets: () => request<SavedSetup<RotationPresetConfig>[]>("GET", "/rotation/presets"),
  saveRotationPreset: (name: string, config: RotationPresetConfig, testId?: number) =>
    request<SavedSetup<RotationPresetConfig>>("POST", "/rotation/presets", { name, config, test_id: testId }),
  /** A bot's tested setup (its rules and test results) as a saved setup of its kind: a name that exists is replaced. */
  saveBotSetup: (id: number, name: string) => request<SavedSetup<Record<string, unknown>>>("POST", `/bots/${id}/setup/save`, { name }),
  deleteRotationPreset: (id: number) => request<void>("DELETE", `/rotation/presets/${id}`),
  // Test batches (parameter sweeps) of a strategy, and each setup they tested (C = the strategy's setup)
  studies: (strategy: StudyStrategy) => request<Study[]>("GET", `/dip/studies?strategy=${strategy}`),
  studyTests: <C>(studyId: number, query: TestQuery) => {
    const params = new URLSearchParams({ offset: String(query.offset), limit: String(query.limit), sort: query.sort, order: query.order });
    if (query.q) params.set("q", query.q);
    for (const flag of ["picks", "profit_both", "beat_both"] as const) if (query[flag]) params.set(flag, "true");
    if (query.filters && Object.keys(query.filters).length) params.set("filters", JSON.stringify(query.filters));
    return request<StudyTestPage<C>>("GET", `/dip/studies/${studyId}/tests?${params}`);
  },
  studySummary: (studyId: number) => request<StudySummary>("GET", `/dip/studies/${studyId}/summary`),
  studyTest: <C>(id: number) => request<StudyTestDetail<C>>("GET", `/dip/tests/${id}`),
  // Email alerts: who gets them and about what; the mail server itself is set in trading-service/.env
  emailAlerts: () => request<EmailAlerts>("GET", "/email-alerts"),
  saveEmailAlerts: (body: EmailAlertsSave) => request<EmailAlerts>("PUT", "/email-alerts", body),
  testEmail: () => request<EmailAlert>("POST", "/email-alerts/test"),
  // Builds today's risk report (the market's and the sectors' news, a minute or so) and queues it as an email
  sendRiskReport: () => request<{ status: string }>("POST", "/risk-report/send"),
  emailHistory: (limit = 50) => request<EmailAlert[]>("GET", `/email-alerts/history?limit=${limit}`),
};
