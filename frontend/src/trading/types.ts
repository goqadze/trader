// Types mirroring trading-service's API (trading-service/app/schemas.py).

export type Mode = "rules" | "llm";
export type BotStatus = "active" | "paused" | "archived";

/** The tunable knobs; same names as the backtest RunConfig so a tested setup deploys 1:1. */
export interface StrategyParams {
  mode: Mode;
  min_confidence: number;
  rebalance_days: number;
  position_pct: number;
  stop_pct: number;
  target_pct: number;
  fee_pct: number;
  slippage_pct: number;
  max_drawdown_pct: number;
}

export interface BotCreate extends StrategyParams {
  name?: string;
  symbol: string;
  broker: string;
  allocated_cash: number;
  confirm_live?: boolean;
}

export type BotUpdate = Partial<StrategyParams> & { name?: string };

export interface Bot extends StrategyParams {
  id: number;
  name: string;
  symbol: string;
  broker: string;
  live: boolean;
  status: BotStatus;
  allocated_cash: number;
  cash: number;
  shares: number;
  entry_price: number | null;
  cost_basis: number;
  stop_price: number | null;
  target_price: number | null;
  realized_pnl: number;
  peak_equity: number;
  benchmark_price: number | null;
  last_price: number | null;
  last_price_at: string | null;
  last_decision_date: string | null;
  created_at: string;
  equity: number;
  return_pct: number;
  unrealized_pnl: number;
  buy_hold_return_pct: number | null;
  pending_order: boolean;
}

export interface Decision {
  id: number;
  created_at: string;
  session_date: string;
  kind: "scheduled" | "manual" | "preview";
  action: "BUY" | "SELL" | "HOLD";
  confidence: number;
  sentiment: string;
  reasoning: string;
  steps: string[];
  price: number | null;
  outcome: string;
}

export interface Order {
  id: number;
  decision_id: number | null;
  created_at: string;
  updated_at: string;
  side: "BUY" | "SELL";
  qty: number;
  reason: "signal" | "stop-loss" | "target" | "manual";
  status: "new" | "submitted" | "filled" | "partially_filled" | "canceled" | "rejected" | "failed";
  client_order_id: string;
  broker_order_id: string | null;
  filled_qty: number;
  avg_price: number | null;
  fee: number;
  pnl: number | null;
  error: string | null;
}

export interface Snapshot {
  day: string;
  equity: number;
  cash: number;
  shares: number;
  price: number;
}

export interface TradingEvent {
  id: number;
  bot_id: number | null;
  created_at: string;
  level: "info" | "warning" | "error";
  kind: string;
  message: string;
}

export interface BrokerInfo {
  name: string;
  label: string;
  live: boolean;
  available: boolean;
  reason: string;
}

export interface TradingStatus {
  now: string;
  market_open: boolean;
  session_open: string | null;
  session_close: string | null;
  next_decision_at: string;
  decision_minutes_before_close: number;
  risk_check_minutes: number;
  scheduler_enabled: boolean;
  scheduler_running: boolean;
  scheduler_last_tick: string | null;
  live_trading_allowed: boolean;
  brokers: BrokerInfo[];
}
