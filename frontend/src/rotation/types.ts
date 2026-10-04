// backtest-service's momentum rotation (POST /runs/rotation, backtest-service/app/engines/rotation.py).

import type { Result } from "../types";

export interface RotationConfig {
  symbols: string[]; // the universe
  start: string;
  end: string;
  top_n: number; // how many of the strongest to hold, in equal parts
  lookback_months: number;
  skip_months: number; // the latest months left out of the momentum (the classic 12-1 skips one)
  abs_filter: boolean; // only hold symbols that rose; a slot without one stays in cash
  slippage_pct?: number;
}

export interface Rebalance {
  date: string;
  held: string[];
  ranking: { symbol: string; momentum_pct: number }[]; // the strongest few that day
}

export interface RotationResult extends Result {
  rebalances: Rebalance[];
  held_at_end: string[];
  benchmark_symbols: string[]; // the universe bought on day one (those with a price then)
  missing_symbols: string[]; // no prices at all
}

export interface RotationRun {
  run_id: string;
  config: RotationConfig;
  status: string;
  result: RotationResult | null;
  error?: string;
}
