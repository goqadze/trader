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
/** How the fall x applies to each symbol: the same for all ("percent"), scaled by its price tier ("price": pricier
 *  stocks need a smaller fall), or drop_atr times its usual daily move ("volatility"). */
export type DropMode = "percent" | "price" | "volatility";
/** drop_mode "price": symbols whose reference price is under up_to (the last tier: null, any price) need share of x. */
export interface PriceTier {
  up_to: number | null;
  share: number;
}

/** Under $100: x; $100-500: 5/6 of x; $500-1000: 2/3 of x; $1000 and up: 1/3 of x (3% -> 3 / 2.5 / 2 / 1%). */
export const DEFAULT_PRICE_TIERS: PriceTier[] = [
  { up_to: 100, share: 1 },
  { up_to: 500, share: 5 / 6 },
  { up_to: 1000, share: 2 / 3 },
  { up_to: null, share: 1 / 3 },
];

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
  drop_mode: DropMode; // ... for every symbol, by its price, or by its usual daily move
  price_tiers: PriceTier[];
  drop_atr: number; // drop_mode "volatility": buy a fall of this many daily moves
  lookback: number; // ... during the last y ...
  lookback_unit: LookbackUnit; // ... days or hours
  drop_from: DropFrom;
  target_mode: TargetMode;
  rise_pct: number; // target_mode "percent" only
  stop_pct: number; // z: sell this far under the buy, and blacklist the symbol ...
  reenable_days: number; // ... for this many trading days; 0 = until you re-enable it
  max_positions: number; // slots: each buy gets 1/max_positions of the equity
  max_hold_days: number; // sell after this many trading days whatever the price; 0 = never
  news: boolean; // bearish news blocks the buy for the rest of the day
  trend_filter: boolean; // only buy dips of symbols in an uptrend (50-day average over the 200-day)
  rebound: boolean; // wait for the turn: buy only once it is back up rebound_pct from its low
  rebound_pct: number; // x1
  fractional: boolean; // buy fractions of a share, so a slot smaller than one share's price still buys
}

export const DEFAULT_RULES: DipRules = {
  interval: "15m",
  drop_pct: 0.05,
  drop_mode: "percent",
  price_tiers: DEFAULT_PRICE_TIERS,
  drop_atr: 1.5,
  lookback: 5,
  lookback_unit: "days",
  drop_from: "high",
  target_mode: "reference",
  rise_pct: 0.05,
  stop_pct: 0.05,
  reenable_days: 0,
  max_positions: 5,
  max_hold_days: 0,
  news: false,
  trend_filter: false,
  rebound: true,
  rebound_pct: 0.01,
  fractional: false,
};

const RULE_KEYS = Object.keys(DEFAULT_RULES) as (keyof DipRules)[];

/** Just the rules out of something bigger (a backtest config, a saved setup); a missing one gets its default. */
export const pickRules = (x: Partial<DipRules>): DipRules =>
  Object.fromEntries(RULE_KEYS.map((k) => [k, x[k] ?? DEFAULT_RULES[k]])) as unknown as DipRules;

const num = (x: number) => (+x.toFixed(2)).toLocaleString("en-US");

/** "under $100", "$100-500", "$1,000 and up": the prices of each tier. */
export const tierSpans = (tiers: PriceTier[]) =>
  tiers.map((t, i) => {
    const low = i ? tiers[i - 1].up_to : null;
    if (t.up_to == null) return low == null ? "any price" : `$${num(low)} and up`;
    return low == null ? `under $${num(t.up_to)}` : `$${num(low)}-${num(t.up_to)}`;
  });

/** "a 3% fall", "a fall of 3% by price (under $100: 3%, ...)", "a fall of 1.5x its usual daily move" */
export const dropText = (r: Pick<DipRules, "drop_pct" | "drop_mode" | "price_tiers" | "drop_atr">) => {
  if (r.drop_mode === "volatility") return `a fall of ${num(r.drop_atr)}× its usual daily move`;
  const x = `${num(r.drop_pct * 100)}%`;
  if (r.drop_mode !== "price") return `a ${x} fall`;
  const spans = tierSpans(r.price_tiers);
  return `a fall of ${x} by price (${r.price_tiers.map((t, i) => `${spans[i]}: ${num(r.drop_pct * t.share * 100)}%`).join(", ")})`;
};

/** "a 5% fall under the 5-day high, once it turns up 1% from its low" */
export const fallText = (r: Pick<DipRules, "drop_pct" | "drop_mode" | "price_tiers" | "drop_atr" | "lookback" | "lookback_unit" | "drop_from" | "rebound" | "rebound_pct">) =>
  `${dropText(r)} under the ${r.lookback}-${r.lookback_unit === "days" ? "day" : "hour"} ${r.drop_from === "high" ? "high" : "start"}` +
  (r.rebound ? `, once it turns up ${+(r.rebound_pct * 100).toFixed(2)}% from its low` : "");

/** "back at that price" / "+3% above the buy" */
export const sellText = (r: Pick<DipRules, "target_mode" | "rise_pct">) =>
  r.target_mode === "reference" ? "back at the price the fall started from" : `${+(r.rise_pct * 100).toFixed(2)}% above the buy`;

// --- The backtest -------------------------------------------------------------------------------------

export interface DipConfig extends DipRules {
  symbols: string[];
  start: string;
  end: string;
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

// --- Saved setups (trading-service /dip/presets) ----------------------------------------------------------

/** A setup saved under a name: the rules and the watchlist, plus the backtest's capital, blacklist and periods. */
export interface DipPresetConfig extends DipRules {
  symbols: string[];
  initial_cash?: number | null;
  practice?: [string, string] | null;
  exam?: [string, string] | null; // null: no exam
  period_months?: number | null; // "the last n months": loaded counting back from today, not the dates above
}

export interface DipPreset {
  id: number;
  name: string;
  config: DipPresetConfig;
  created_at: string;
  updated_at: string;
}

// --- The live bot ---------------------------------------------------------------------------------------

export interface DipBotCreate extends DipRules {
  name?: string;
  broker: string;
  confirm_live?: boolean; // a real-money broker: you said yes to real orders
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
  reenable_on: string | null; // a stop's blacklist ends that trading day (YYYY-MM-DD); null: when you re-enable it
  checked_at: string | null;
  last_price: number | null;
  reference_price: number | null; // the window's high (or start)
  drop: number | null; // 0.06 = 6% under the reference
  buy_below: number | null; // in the buy zone at or under this price
  buy_drop: number | null; // ... a fall this big (its own with drop_mode "price" or "volatility"); null: not known yet
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

// --- Test batches (parameter sweeps) ------------------------------------------------------------------

/** A test's numbers in one window (backtest-service's dip result, trimmed to what the grid shows). */
export interface DipTestNumbers {
  total_return_pct: number;
  buy_hold_return_pct: number;
  alpha_vs_buy_hold_pct: number;
  max_drawdown_pct: number;
  buy_hold_max_drawdown_pct: number;
  sharpe: number | null;
  num_trades: number;
  win_rate_pct: number;
  profit_factor: number | null;
  avg_trade_pct: number | null;
  worst_trade_pct: number | null;
  avg_hold_days: number | null;
  exposure_pct: number;
  stop_exits: number;
  open_position: number;
  unrealized_pnl: number;
  final_equity: number;
  grade: "paper" | "weak" | "no";
  error?: string;
}

/** A batch of tests run together: what was varied, over which windows ({period: [start, end]}, in the grid's order). */
export interface DipStudy {
  id: number;
  name: string;
  description: string;
  periods: Record<string, [string, string]>;
  created_at: string;
  tests: number;
}

export interface DipTest {
  id: number;
  study_id: number;
  code: string;
  name: string;
  watchlist: string;
  config: DipPresetConfig;
  results: Record<string, DipTestNumbers>;
  score: number | null;
  pick: number | null; // recommended: 1 = the best pick
  note: string;
  extra?: string | null; // a filter tested on top of the rules (the bots don't have it yet)
  quarters?: DipTestQuarters | null; // twelve separate 3-month tests (the best setups only)
}

/** Twelve 3-month tests back to back: how many made money, the worst, each one's return. */
export interface DipTestQuarters {
  won: number;
  of: number;
  worst: number;
  returns: number[];
}

export interface DipTestPeriodDetail {
  curve: [string, number, number][]; // [date, equity, buy & hold]
  by_symbol: { symbol: string; trades: number; wins: number; pnl: number; stops: number; status: string }[];
  open_positions: { symbol: string; entry: number; price: number; unrealized_pnl: number }[];
  blacklisted_at_end: string[];
}

export interface DipTestDetail extends DipTest {
  detail: Record<string, DipTestPeriodDetail>;
}
