// The dip buyer: its backtest (backtest-service POST /runs/dip, app/engines/dip.py) and its live bot (trading-service
// POST /bots/dip, app/dip.py). Both use the same rule names, so a tested setup deploys 1:1.

import type { Result } from "../types";

/** How often it checks: at the end of each 5/15/30/60-minute bar, or once a day before the close. */
export type DipInterval = "5m" | "15m" | "30m" | "1h" | "1d";
export type LookbackUnit = "days" | "hours";
/** The price a fall is measured from: the window's highest close (it rose, then turned) or its first close. */
export type DropFrom = "high" | "start";
/** Sell back at that price ("reference"), or a fixed rise above the buy ("percent"). */
export type TargetMode = "reference" | "percent";

export const INTERVALS: { value: DipInterval; label: string; minutes: number }[] = [
  { value: "5m", label: "every 5 minutes", minutes: 5 },
  { value: "15m", label: "every 15 minutes", minutes: 15 },
  { value: "30m", label: "every 30 minutes", minutes: 30 },
  { value: "1h", label: "every hour", minutes: 60 },
  { value: "1d", label: "once a day (before the close)", minutes: 390 },
];

/** The rules, shared by the backtest and the bot. Fractions on the wire (0.05 = 5%). */
export interface DipRules {
  interval: DipInterval;
  drop_pct: number; // x: buy a fall this big ...
  lookback: number; // ... during the last y ...
  lookback_unit: LookbackUnit; // ... days or hours
  drop_from: DropFrom;
  target_mode: TargetMode;
  rise_pct: number; // target_mode "percent" only
  stop_pct: number; // z: sell this far under the buy, and blacklist the symbol
  max_positions: number; // slots: each buy gets 1/max_positions of the equity
  max_hold_days: number; // sell after this many trading days whatever the price; 0 = never
  news: boolean; // bearish news blocks the buy for the rest of the day
  trend_filter: boolean; // only buy dips of symbols in an uptrend (50-day average over the 200-day)
  rebound: boolean; // wait for the turn: buy only once it is back up rebound_pct from its low
  rebound_pct: number; // x1
}

export const DEFAULT_RULES: DipRules = {
  interval: "15m",
  drop_pct: 0.05,
  lookback: 5,
  lookback_unit: "days",
  drop_from: "high",
  target_mode: "reference",
  rise_pct: 0.05,
  stop_pct: 0.05,
  max_positions: 5,
  max_hold_days: 0,
  news: false,
  trend_filter: false,
  rebound: true,
  rebound_pct: 0.01,
};

/** "a 5% fall under the 5-day high, once it turns up 1% from its low" */
export const fallText = (r: Pick<DipRules, "drop_pct" | "lookback" | "lookback_unit" | "drop_from" | "rebound" | "rebound_pct">) =>
  `a ${+(r.drop_pct * 100).toFixed(2)}% fall under the ${r.lookback}-${r.lookback_unit === "days" ? "day" : "hour"} ${r.drop_from === "high" ? "high" : "start"}` +
  (r.rebound ? `, once it turns up ${+(r.rebound_pct * 100).toFixed(2)}% from its low` : "");

/** "back at that price" / "+3% above the buy" */
export const sellText = (r: Pick<DipRules, "target_mode" | "rise_pct">) =>
  r.target_mode === "reference" ? "back at the price the fall started from" : `${+(r.rise_pct * 100).toFixed(2)}% above the buy`;

// --- The backtest -------------------------------------------------------------------------------------

export interface DipConfig extends DipRules {
  symbols: string[];
  start: string;
  end: string;
  reenable_days: number; // backtest only: stands in for you re-enabling a blacklisted symbol; 0 = never
  initial_cash?: number;
  slippage_pct?: number;
  fee_pct?: number;
  max_drawdown_pct?: number;
}

export interface DipTrade {
  side: "BUY" | "SELL";
  symbol: string;
  date: string;
  time: string; // "HH:MM" New York for intraday checks, "" for daily ones
  price: number;
  shares: number;
  fee: number;
  // BUYs
  drop_pct?: number; // how far under the reference it was bought (negative)
  reference?: number;
  target?: number;
  stop?: number;
  // SELLs: the round trip
  pnl?: number;
  pnl_pct?: number;
  hold_days?: number;
  reason?: "target" | "stop-loss" | "time";
  entry?: number;
  // For the charts: this leg's moment (ISO; a date for daily checks), where the fall started (the window's high or
  // start), and the low: on a BUY that waited for the turn, the low it turned up from; on a SELL, the lowest close
  // between the fall's start and the sale
  t?: string;
  buy_t?: string; // SELLs: when it was bought
  peak_t?: string;
  peak_price?: number;
  low_t?: string;
  low_price?: number;
  rebound_pct?: number; // BUYs that waited for the turn: how far up from the low it was bought
}

export interface DipEvent {
  date: string;
  time: string;
  symbol: string;
  kind: "blacklisted" | "reenabled" | "news" | "breaker";
  text: string;
}

export interface DipSymbolStats {
  symbol: string;
  trades: number;
  wins: number;
  pnl: number;
  stops: number;
  status: "watching" | "held" | "blacklisted"; // at the end of the run
}

export interface DipResult extends Omit<Result, "trades"> {
  trades: DipTrade[];
  open_positions: {
    symbol: string; shares: number; entry: number; target: number; stop: number; price: number; unrealized_pnl: number;
    buy_t: string; peak_t: string; peak_price: number;
  }[];
  time_exits: number;
  by_symbol: DipSymbolStats[];
  blacklisted_at_end: string[];
  events: DipEvent[];
  // dips = falls into the buy zone; the others count (symbol, day) pairs not bought for that reason
  // rebounds = falls that turned up rebound_pct from their low (when waiting for the turn)
  dip_stats: { dips: number; bought: number; no_slot: number; too_small: number; news_vetoes: number; trend_skips: number; blacklisted_skips: number; rebounds: number };
  benchmark_symbols: string[];
  missing_symbols: string[];
}

export interface DipRun {
  run_id: string;
  config: DipConfig;
  status: string;
  result: DipResult | null;
  error?: string;
}

// --- The live bot ---------------------------------------------------------------------------------------

export interface DipBotCreate extends DipRules {
  name?: string;
  broker: string;
  allocated_cash: number;
  symbols: string[];
  fee_pct: number;
  slippage_pct: number;
  max_drawdown_pct: number;
}

export type DipBotUpdate = Partial<DipRules & Pick<DipBotCreate, "name" | "fee_pct" | "slippage_pct" | "max_drawdown_pct">>;

/** One symbol on a dip bot's watchlist, as of its last check. */
export interface WatchItem {
  symbol: string;
  status: "watching" | "blacklisted";
  added_at: string;
  blacklisted_at: string | null;
  blacklist_reason: string | null;
  checked_at: string | null;
  last_price: number | null;
  reference_price: number | null; // the window's high (or start)
  drop: number | null; // 0.06 = 6% under the reference
  buy_below: number | null; // in the buy zone at or under this price
  in_zone: boolean;
  held: boolean;
  // The fall being followed (from the buy zone until bought or back up): where it started, since when, its low, the turn
  dip_reference: number | null;
  armed_at: string | null;
  trough_price: number | null;
  trough_at: string | null;
  turned_at: string | null;
  rebound_at: number | null; // waiting for the turn: bought at or above this price
  sold_on: string | null;
  news_sentiment: string | null;
  news_at: string | null;
  news_blocked_on: string | null;
  trend_ok: boolean | null;
}

/** A recommendation: fell into the buy zone (down), turned up from its low (rebound: bearish turned bullish), back up
 *  (up), stopped out (stop), held too long (time), bearish news (news). `outcome` says what the bot did about it. */
export interface DipSignal {
  id: number;
  bot_id: number;
  created_at: string;
  symbol: string;
  kind: "down" | "rebound" | "up" | "stop" | "time" | "news";
  price: number | null;
  reference_price: number | null;
  change_pct: number | null;
  message: string;
  outcome: string;
}
