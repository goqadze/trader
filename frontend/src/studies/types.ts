// Test batches (parameter sweeps) and saved setups, shared by the strategies that have them: the dip buyer and the
// momentum rotation (trading-service /dip/studies?strategy=, /dip/presets, /rotation/presets). C = a strategy's setup
// as a saved setup keeps it (DipPresetConfig, RotationPresetConfig).

import type { BotSetup } from "../trading/types";

export type StudyStrategy = "dip" | "rotation";

/** A setup saved under a name. */
export interface SavedSetup<C> {
  id: number;
  name: string;
  strategy?: StudyStrategy;
  config: C;
  // The tested setup it was saved from (a bot's Tested setup popup, Test results), with its results: a bot started from
  // it or switched to it keeps this. null = saved from a form
  setup?: Omit<BotSetup, "started_at" | "switched" | "all"> | null;
  created_at: string;
  updated_at: string;
}

/** A test's numbers in one window (backtest-service's result, trimmed to what the grid shows). */
export interface TestNumbers {
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
export interface Study {
  id: number;
  name: string;
  strategy?: StudyStrategy;
  description: string;
  periods: Record<string, [string, string]>;
  created_at: string;
  tests: number;
}

/** Separate short tests back to back (the dip buyer's twelve quarters, the rotation's years): how many made money,
 *  the worst, each one's return. */
export interface TestQuarters {
  won: number;
  of: number;
  worst: number;
  returns: number[];
}

export interface StudyTest<C> {
  id: number;
  study_id: number;
  code: string;
  name: string;
  watchlist: string;
  config: C;
  results: Record<string, TestNumbers>;
  score: number | null;
  pick: number | null; // recommended: 1 = the best pick
  note: string;
  extra?: string | null; // a filter tested on top of the rules (the bots don't have it yet)
  quarters?: TestQuarters | null; // separate short tests (the best setups only)
  sectors?: string[]; // its symbols' sector badges, as a bot with them would get (only in a page of the grid)
}

/** One page of a study's grid (the server filters, sorts and pages it), and how many tests the filters leave. */
export interface StudyTestPage<C> {
  total: number;
  items: StudyTest<C>[];
}

/** What a page of the grid is asked with: GET /dip/studies/{id}/tests. */
export interface TestQuery {
  offset: number;
  limit: number;
  sort: string; // "pick", "score", "calm", "all", "quarters", "name", "watchlist", "<period>-ret|dd|trades" or a rule key
  order: "asc" | "desc";
  q?: string;
  picks?: boolean;
  profit_both?: boolean;
  beat_both?: boolean;
  filters?: Record<string, (string | number)[]>; // "watchlist", "extra" (— = none), "sector" (any of them) or a rule key -> the values kept
}

/** The grid's frame: counts, each column's filter choices, and each rule's like-for-like effect on the score. */
export interface StudySummary {
  tests: number;
  made_money_both: number;
  beat_hold_both: number;
  watchlists: string[];
  sectors: string[]; // the Sectors filter's choices: the tests' badges, the most common first
  extras: string[]; // the Filter column's values ("—" = none); empty when no test has one
  rules: Record<string, { value: number; text: string }[]>; // only the rules the tests differ in
  has_quarters: boolean;
  effects: { key: string; cells: { label: string; median: number; n: number }[] }[];
}

export interface PeriodDetail {
  curve: [string, number, number][]; // [date, equity, buy & hold]
  by_symbol: { symbol: string; trades: number; wins: number; pnl: number; stops: number; status: string }[];
  open_positions: { symbol: string; entry: number; price: number; unrealized_pnl: number }[];
  blacklisted_at_end: string[];
}

export interface StudyTestDetail<C> extends StudyTest<C> {
  detail: Record<string, PeriodDetail>;
}
