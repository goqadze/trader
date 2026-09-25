// Shared domain types for the backtest dashboard.
// These mirror the JSON shapes produced by backtest-service.

import type { StrategyId } from "./strategies";

export type Action = "BUY" | "SELL" | "HOLD" | "hold"; // "hold" = carry day (non-decision)
export type RunStatus = "idle" | "starting" | "running" | "done" | "error";

/** Config sent to POST /runs (matches backtest-service RunConfig). */
export interface RunConfig {
  symbol: string;
  start: string; // YYYY-MM-DD
  end: string; // YYYY-MM-DD
  initial_cash: number;
  min_confidence: number;
  rebalance_days: number;
  position_pct: number;
  stop_pct?: number; // stop-loss distance below entry as a fraction (0.04 = 4%); omitted = server default
  target_pct?: number; // take-profit distance above entry as a fraction
  strategy: StrategyId;
}

export interface Trade {
  side: "BUY" | "SELL";
  date: string;
  price: number;
  shares: number;
  fee?: number;
  pnl?: number; // SELLs only: round-trip profit net of fees and slippage
  pnl_pct?: number; // ... as a % of what the entry cost
  hold_days?: number; // trading days between entry and exit
  reason?: "signal" | "stop-loss" | "target";
}

/** Final result payload carried by the "done" event. */
export interface Result {
  final_equity: number;
  total_return_pct: number;
  buy_hold_return_pct: number;
  alpha_vs_buy_hold_pct?: number; // strategy return minus buy & hold
  beat_buy_hold?: boolean;
  max_drawdown_pct: number;
  num_trades: number;
  win_rate_pct: number;
  trades: Trade[];
  equity_curve: { date: string; equity: number; price: number; position?: number }[];
  // Comparison metrics (backtest-service's engine). null = not computable (no trades, no movement, ...).
  buy_hold_max_drawdown_pct?: number;
  volatility_pct?: number | null; // annualized
  sharpe?: number | null; // annualized, risk-free rate 0
  sortino?: number | null;
  buy_hold_volatility_pct?: number | null;
  buy_hold_sharpe?: number | null;
  buy_hold_sortino?: number | null;
  return_over_drawdown?: number | null;
  profit_factor?: number | null; // null with winning trades = no losses (infinite)
  avg_trade_pct?: number | null;
  avg_win_pct?: number | null;
  avg_loss_pct?: number | null;
  best_trade_pct?: number | null;
  worst_trade_pct?: number | null;
  avg_hold_days?: number | null;
  exposure_pct?: number;
  stop_exits?: number;
  target_exits?: number;
  total_fees?: number;
  open_position?: number;
  unrealized_pnl?: number;
  signals?: Record<"BUY" | "SELL" | "HOLD", number>;
  decision_errors?: number;
  no_news_decisions?: number;
}

// --- WebSocket events (discriminated union on `type`) ---
export interface StartEvent {
  type: "start";
  seq: number;
  symbol: string;
  total: number;
  initial_cash: number;
  first_price: number;
}
export interface StepEvent {
  type: "step";
  seq: number;
  i: number;
  total: number;
  date: string;
  action: Action;
  confidence: number;
  price: number;
  cash: number;
  position: number;
  equity: number;
  sentiment: string;
  reasoning: string;
}
export interface TradeEvent {
  type: "trade";
  seq: number;
  side: "BUY" | "SELL";
  date: string;
  price: number;
  shares: number;
  pnl?: number;
}
export interface DoneEvent {
  type: "done";
  seq: number;
  result: Result;
}
export interface ErrorEvent {
  type: "error";
  seq: number;
  message: string;
}
export type RunEvent = StartEvent | StepEvent | TradeEvent | DoneEvent | ErrorEvent;

/** One row in the activity log table. */
export interface LogRow {
  key: number;
  date: string;
  action: Action;
  confidence: number;
  price: number;
  equity: number;
  sentiment: string; // bullish | bearish | neutral | unavailable
  reasoning: string; // the agent's "show your work" explanation for this decision
}

/** One point on the equity chart. */
export interface ChartPoint {
  date: string;
  strategy: number;
  buyhold: number;
}

/** The whole UI state for one run, rendered by the components. */
export interface BacktestState {
  status: RunStatus;
  symbol: string;
  total: number;
  i: number;
  equity: number | null;
  returnPct: number | null;
  position: number;
  trades: number;
  chart: ChartPoint[];
  log: LogRow[];
  result: Result | null;
  error: string | null;
  config: RunConfig | null; // what was run, so a good result can be deployed as a trading bot
  initialCash: number; // from the start event: return % and the buy & hold line are derived from these
  bhShares: number;
}
