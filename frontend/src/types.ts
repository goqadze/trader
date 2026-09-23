// Shared domain types for the backtest dashboard.
// These mirror the JSON shapes produced by backtest-service.

export type Action = "BUY" | "SELL" | "HOLD" | "hold"; // "hold" = carry day (non-decision)
export type Mode = "rules" | "llm";
export type Engine = "simple" | "nautilus";
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
  engine: Engine;
  mode: Mode;
}

export interface Trade {
  side: "BUY" | "SELL";
  date: string;
  price: number;
  shares: number;
  pnl?: number;
}

/** Final result payload carried by the "done" event. */
export interface Result {
  final_equity: number;
  total_return_pct: number;
  buy_hold_return_pct: number;
  alpha_vs_buy_hold_pct?: number; // strategy return minus buy & hold
  max_drawdown_pct: number;
  num_trades: number;
  win_rate_pct: number;
  trades: Trade[];
  equity_curve: { date: string; equity: number; price: number }[];
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
}
