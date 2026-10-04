// Shapes of backtest-service's /scans API (backtest-service/app/scan.py).

import type { StrategyId } from "../strategies";
import type { RunConfig } from "../types";
import type { Verdict } from "../verdict";

/** How each strategy trades in a scan: a RunConfig without the symbol and window. */
export type RunSettings = Omit<RunConfig, "symbol" | "start" | "end">;

export interface Period {
  start: string; // YYYY-MM-DD
  end: string;
}

export interface ScanConfig {
  symbols: string[];
  runs: RunSettings[]; // one per strategy
  practice: Period;
  exam: Period | null; // null = practice only
  exam_all: boolean; // false: only the combinations that passed practice sit the exam
}

export type ScanRunStatus = "pending" | "running" | "done" | "error" | "stopped";

export interface ScanRunView {
  run_id: string;
  status: ScanRunStatus;
  progress: number; // 0..1
  error: string | null;
  summary: {
    total_return_pct: number;
    buy_hold_return_pct: number;
    alpha_vs_buy_hold_pct: number;
    max_drawdown_pct: number;
    sharpe: number | null;
    buy_hold_sharpe: number | null;
    num_trades: number;
    win_rate_pct: number;
    profit_factor: number | null;
    avg_trade_pct: number | null;
    exposure_pct: number;
    decision_errors: number;
  } | null;
  verdict: Verdict | null;
}

export interface ScanCell {
  symbol: string;
  strategy: StrategyId;
  practice: ScanRunView | null;
  exam: ScanRunView | null;
  exam_note: string | null; // why there's no exam run
}

export interface ScanView {
  scan_id: string;
  created_at: string;
  status: "running" | "done" | "stopped";
  config: ScanConfig;
  total: number;
  finished: number;
  cells: ScanCell[];
}

export interface ScanSummary {
  scan_id: string;
  created_at: string;
  status: ScanView["status"];
  total: number;
  finished: number;
  symbols: string[];
  strategies: StrategyId[];
  news: boolean;
}
