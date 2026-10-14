// Typed client for the trading journal (trading-service journal.py, behind /api/trading/journal). Every call answers
// with the signed-in user's own journal only.

import { ApiError, request as http } from "../http";
import type {
  JournalCalendarDay, JournalDay, JournalDayFields, JournalFilters, JournalImage, JournalImportBot, JournalNote, JournalNoteIn,
  JournalOptions, JournalPage, JournalStats, JournalTradeCreate, JournalTradeDetail, JournalTradeUpdate,
} from "./types";

const BASE = "/api/trading/journal";

const request = <T>(method: string, path: string, body?: unknown) => http<T>(method, `${BASE}${path}`, body);

/** The filters as a query string (empty values left out). */
export function filterQuery(f: JournalFilters, extra: Record<string, string | number> = {}): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...f, ...extra })) if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  const s = params.toString();
  return s ? `?${s}` : "";
}

export type TradeSort = "day" | "pnl" | "r" | "symbol";

export const journalApi = {
  trades: (f: JournalFilters, page: { offset: number; limit: number; sort?: TradeSort; order?: "asc" | "desc" }) =>
    request<JournalPage>("GET", `/trades${filterQuery(f, { offset: page.offset, limit: page.limit, sort: page.sort ?? "day", order: page.order ?? "desc" })}`),
  trade: (id: number) => request<JournalTradeDetail>("GET", `/trades/${id}`),
  createTrade: (body: JournalTradeCreate) => request<JournalTradeDetail>("POST", "/trades", body),
  updateTrade: (id: number, body: JournalTradeUpdate) => request<JournalTradeDetail>("PATCH", `/trades/${id}`, body),
  deleteTrade: (id: number) => request<void>("DELETE", `/trades/${id}`),
  /** Where the browser downloads the filtered trades as a spreadsheet. */
  csvUrl: (f: JournalFilters) => `${BASE}/trades.csv${filterQuery(f)}`,
  addNote: (body: JournalNoteIn) => request<JournalNote>("POST", "/notes", body),
  updateNote: (id: number, body: Partial<JournalNoteIn>) => request<JournalNote>("PATCH", `/notes/${id}`, body),
  deleteNote: (id: number) => request<void>("DELETE", `/notes/${id}`),
  notes: (q: { start?: string; end?: string; stage?: string; limit?: number }) =>
    request<JournalNote[]>("GET", `/notes${filterQuery({}, q as Record<string, string | number>)}`),
  imageUrl: (id: number) => `${BASE}/images/${id}`,
  deleteImage: (id: number) => request<void>("DELETE", `/images/${id}`),
  /** The request's body is the image file itself. */
  uploadImage: async (tradeId: number, file: Blob, caption = "") => {
    const res = await fetch(`${BASE}/trades/${tradeId}/images?caption=${encodeURIComponent(caption)}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        msg = (await res.json()).detail ?? msg;
      } catch {
        if (res.status === 413) msg = "The image is too big to upload";
      }
      throw new ApiError(res.status, msg);
    }
    return (await res.json()) as JournalImage;
  },
  day: (day: string) => request<JournalDay>("GET", `/days/${day}`),
  saveDay: (day: string, body: JournalDayFields) => request<JournalDay>("PUT", `/days/${day}`, body),
  calendar: (start: string, end: string) => request<JournalCalendarDay[]>("GET", `/calendar?start=${start}&end=${end}`),
  stats: (f: JournalFilters) => request<JournalStats>("GET", `/stats${filterQuery(f)}`),
  options: () => request<JournalOptions>("GET", "/options"),
  importChoices: () => request<JournalImportBot[]>("GET", "/import/bots"),
  importBots: (botIds: number[]) => request<{ imported: number; already: number }>("POST", "/import", { bot_ids: botIds }),
};
