// Types mirroring trading-service's API (trading-service/app/schemas.py).

import type { DipInterval, WatchItem } from "../dip/types";
import type { StrategyId } from "../strategies";

/** When a bot checks on a decision day: 30 min before the close, 30 min after the open, or both. */
export type DecideAt = "close" | "open" | "both";
export type BotStatus = "active" | "paused" | "archived";

/** The tunable knobs; same names as the backtest RunConfig so a tested setup deploys 1:1. */
export interface StrategyParams {
  strategy: StrategyId;
  min_confidence: number;
  rebalance_days: number;
  decide_at: DecideAt; // when a decision day decides; backtests can replay all three
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

/** A momentum rotation bot's strategy: one bot holding the strongest few of a universe (trading-service rotation.py). */
export const ROTATION = "momentum_rotation" as const;
/** A dip buyer's strategy: one bot watching a list of symbols, buying falls and selling them back up (trading-service dip.py). */
export const DIP = "dip_buyer" as const;

/** A rotation bot: the backtest's rotation settings (same names) plus the bot's broker and capital. */
export interface RotationBotCreate {
  name?: string;
  broker: string;
  allocated_cash: number;
  universe: string[];
  top_n: number;
  lookback_months: number;
  skip_months: number;
  abs_filter: boolean;
  fee_pct: number;
  slippage_pct: number;
  max_drawdown_pct: number;
}

/** A rotation bot changes only these: its universe and rules are fixed for its life. */
export type RotationBotUpdate = Partial<Pick<RotationBotCreate, "name" | "fee_pct" | "slippage_pct" | "max_drawdown_pct">>;

/** One symbol a rotation or dip bot holds. */
export interface Holding {
  symbol: string;
  shares: number;
  cost_basis: number;
  last_price: number | null;
  last_price_at: string | null;
  value: number;
  weight_pct: number; // of the bot's equity
  unrealized_pnl: number;
  opened_at: string | null;
  // Dip bots only: the exit levels, set from the buy's fill
  entry_price: number | null;
  reference_price: number | null; // the price the fall started from
  target_price: number | null;
  stop_price: number | null;
  on_watchlist: boolean; // false: removed from the watchlist, held until it exits
}

export interface Bot extends Omit<StrategyParams, "strategy"> {
  strategy: StrategyId | typeof ROTATION | typeof DIP;
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
  last_decision_at: string | null;
  created_at: string;
  next_decision_at: string | null; // null = paused/archived: no scheduled decisions
  equity: number;
  return_pct: number;
  unrealized_pnl: number;
  buy_hold_return_pct: number | null;
  pending_order: boolean; // a market order is still working at the broker
  stop_at_broker: boolean; // the stop-loss rests at the broker as a real order (Alpaca), so it works while this app is off
  // Rotation bots only (null / empty on the others). Their benchmark_price / last_price are what the universe held in
  // equal parts since the start is worth, beginning at allocated_cash.
  universe: string[] | null;
  top_n: number | null;
  lookback_months: number | null;
  skip_months: number | null;
  abs_filter: boolean | null;
  holdings: Holding[];
  rebalancing: boolean; // a rebalance's orders are still going out (sells first, then the buys)
  // Dip buyers only (null / empty on the others). Their `universe` is the starting watchlist: the benchmark holds it.
  interval: DipInterval | null;
  drop_pct: number | null;
  lookback: number | null;
  lookback_unit: "days" | "hours" | null;
  drop_from: "high" | "start" | null;
  target_mode: "reference" | "percent" | null;
  rise_pct: number | null;
  max_positions: number | null;
  max_hold_days: number | null;
  news: boolean | null;
  trend_filter: boolean | null;
  rebound: boolean | null; // null on dip bots from before the option: they buy at once
  rebound_pct: number | null;
  fractional: boolean | null; // null on dip bots from before the option: whole shares
  watchlist: WatchItem[];
}

export interface Decision {
  id: number;
  created_at: string;
  session_date: string;
  kind: "scheduled" | "manual" | "preview";
  action: "BUY" | "SELL" | "HOLD" | "ROTATE" | "TRADE"; // ROTATE: a rotation bot changed what it holds; TRADE: a dip check bought and sold
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
  symbol: string | null; // null on orders from before it was stored: the bot's symbol
  order_type: "market" | "stop"; // stop = the stop-loss resting at the broker until the price falls to it
  stop_price: number | null;
  qty: number;
  // rotation: in or out; rebalance: a trim or top-up; dip: a dip bot's buy; time: held its max days
  reason: "signal" | "stop-loss" | "target" | "manual" | "rotation" | "rebalance" | "dip" | "time";
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
  decision_minutes_after_open: number;
  risk_check_minutes: number;
  scheduler_enabled: boolean;
  scheduler_running: boolean;
  scheduler_last_tick: string | null;
  live_trading_allowed: boolean;
  brokers: BrokerInfo[];
}

// --- Email alerts (trading-service notify.py, /email-alerts) ---

export type NotifyCategory = "trades" | "risk" | "problems" | "signals";

export interface EmailAlertsSave {
  enabled: boolean;
  recipients: string[];
  categories: NotifyCategory[];
}

export interface EmailAlerts extends EmailAlertsSave {
  smtp_configured: boolean; // SMTP_HOST set in trading-service/.env: without it nothing is sent
  smtp_server: string; // "smtp.gmail.com:587 (starttls)"
  sender: string;
  dashboard_url: string; // where the emails' links point
  available: { key: NotifyCategory; label: string; description: string }[];
}

/** One email alert: queued with its event, then sent by a background loop (several at once go out as one email). */
export interface EmailAlert {
  id: number;
  created_at: string;
  bot_id: number | null;
  category: NotifyCategory | "test";
  kind: string;
  subject: string;
  body: string;
  status: "pending" | "sent" | "failed" | "skipped";
  attempts: number;
  next_attempt_at: string | null;
  sent_at: string | null;
  error: string | null;
}
