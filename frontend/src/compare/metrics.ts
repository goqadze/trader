// The measurements a strategy comparison shows, with what each means and which direction is better.
// Values come from backtest-service's engine (SimplePortfolioEngine._metrics).

import type { Result } from "../types";

export interface Metric {
  key: string;
  title: string;
  help: string; // plain-words explanation, shown on hover
  value: (r: Result) => number | null | undefined; // what is sorted and compared
  format: (v: number | null | undefined, r: Result) => string;
  better?: "high" | "low"; // highlight the best run; none = neither direction is "better"
  buyHold?: (r: Result) => number | null | undefined; // the buy & hold benchmark's value, if it has one
  table?: boolean; // a summary-table column (every metric is in the per-strategy details)
}

const num = (v: number | null | undefined, digits = 2) => (v == null ? "—" : v.toFixed(digits));
const pct = (v: number | null | undefined, digits = 2) => (v == null ? "—" : `${v.toFixed(digits)}%`);
const signedPct = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);
const money = (v: number | null | undefined) => (v == null ? "—" : `$${Math.round(v).toLocaleString()}`);

export const METRICS: Metric[] = [
  {
    key: "total_return_pct", title: "Return", table: true, better: "high",
    help: "Gain or loss of the whole account over the window, after fees and slippage. Includes a position still open at the end.",
    value: (r) => r.total_return_pct, format: signedPct, buyHold: (r) => r.buy_hold_return_pct,
  },
  {
    key: "alpha_vs_buy_hold_pct", title: "vs Buy & Hold", table: true, better: "high",
    help: "Return minus buy & hold's. Positive = the strategy did better than just buying on day 1 and waiting.",
    value: (r) => r.alpha_vs_buy_hold_pct, format: signedPct,
  },
  {
    key: "max_drawdown_pct", title: "Max drawdown", table: true, better: "high",
    help: "The biggest drop of the account from a previous high, in %. Closer to 0 = less pain along the way.",
    value: (r) => r.max_drawdown_pct, format: (v) => pct(v), buyHold: (r) => r.buy_hold_max_drawdown_pct,
  },
  {
    key: "sharpe", title: "Sharpe", table: true, better: "high",
    help: "Return per unit of day-to-day swings, annualized. Above 1 is good, above 2 very good. Noisy over a few weeks.",
    value: (r) => r.sharpe, format: (v) => num(v), buyHold: (r) => r.buy_hold_sharpe,
  },
  {
    key: "sortino", title: "Sortino", table: true, better: "high",
    help: "Like Sharpe, but only downward swings count as risk. Empty = the account never had a down day.",
    value: (r) => r.sortino, format: (v) => num(v), buyHold: (r) => r.buy_hold_sortino,
  },
  {
    key: "return_over_drawdown", title: "Return / DD", table: true, better: "high",
    help: "Return divided by the worst drawdown: how much was earned per % of drop suffered. Empty = never fell.",
    value: (r) => r.return_over_drawdown, format: (v) => num(v),
  },
  {
    key: "volatility_pct", title: "Volatility", table: true, better: "low",
    help: "How much the account value swings day to day, annualized. Lower = a smoother ride (cash has 0).",
    value: (r) => r.volatility_pct, format: (v) => pct(v, 1), buyHold: (r) => r.buy_hold_volatility_pct,
  },
  {
    key: "win_rate_pct", title: "Win rate", table: true, better: "high",
    help: "Share of closed trades that made money after costs.",
    value: (r) => (r.num_trades ? r.win_rate_pct : null), format: (v) => pct(v, 0),
  },
  {
    key: "num_trades", title: "Trades", table: true,
    help: "Closed round trips (a buy, then a sell). A position still open at the end isn't counted here.",
    value: (r) => r.num_trades, format: (v) => num(v, 0),
  },
  {
    key: "profit_factor", title: "Profit factor", table: true, better: "high",
    help: "Money made on winning trades ÷ money lost on losing ones. Above 1 makes money; ∞ = no losing trade yet.",
    // null means "no losses": infinite when there were wins, undefined when there were no trades at all
    value: (r) => (r.profit_factor != null ? r.profit_factor : r.num_trades && r.win_rate_pct > 0 ? Infinity : null),
    format: (v) => (v === Infinity ? "∞" : num(v)),
  },
  {
    key: "avg_trade_pct", title: "Avg trade", table: true, better: "high",
    help: "The average round trip after costs (the expectancy): what one more trade is worth on average.",
    value: (r) => r.avg_trade_pct, format: signedPct,
  },
  {
    key: "best_trade_pct", title: "Best trade", table: true, better: "high",
    help: "The best closed round trip, in %.", value: (r) => r.best_trade_pct, format: signedPct,
  },
  {
    key: "worst_trade_pct", title: "Worst trade", table: true, better: "high",
    help: "The worst closed round trip, in %.", value: (r) => r.worst_trade_pct, format: signedPct,
  },
  {
    key: "avg_win_pct", title: "Avg win", better: "high",
    help: "The average winning trade, in %.", value: (r) => r.avg_win_pct, format: signedPct,
  },
  {
    key: "avg_loss_pct", title: "Avg loss", better: "high",
    help: "The average losing trade, in % (closer to 0 is better).", value: (r) => r.avg_loss_pct, format: signedPct,
  },
  {
    key: "avg_hold_days", title: "Avg hold", table: true,
    help: "Trading days a round trip lasted on average.", value: (r) => r.avg_hold_days, format: (v) => (v == null ? "—" : `${v.toFixed(1)} d`),
  },
  {
    key: "exposure_pct", title: "Time in market", table: true,
    help: "Share of days that ended holding the stock. Low = mostly in cash: less risk, but also less of the upside.",
    value: (r) => r.exposure_pct, format: (v) => pct(v, 0), buyHold: () => 100,
  },
  {
    key: "final_equity", title: "Final equity", better: "high",
    help: "Account value at the end of the window.", value: (r) => r.final_equity, format: money,
  },
  {
    key: "exits", title: "Exits (stop / target / signal)",
    help: "Why positions were closed: the stop-loss, the take-profit, or the strategy's own sell signal.",
    value: (r) => r.num_trades,
    format: (_v, r) => {
      const stop = r.stop_exits ?? 0, target = r.target_exits ?? 0;
      return `${stop} / ${target} / ${r.num_trades - stop - target}`;
    },
  },
  {
    key: "signals", title: "Signals (buy / sell / hold)",
    help: "What the strategy said on its decision days, before the min-confidence filter. All holds = its setup never appeared.",
    value: (r) => r.signals?.BUY ?? null,
    format: (_v, r) => (r.signals ? `${r.signals.BUY} / ${r.signals.SELL} / ${r.signals.HOLD}` : "—"),
  },
  {
    key: "open_position", title: "Open at the end",
    help: "Shares still held on the last day, and their unrealized gain or loss (already part of the return).",
    value: (r) => r.open_position ?? 0,
    format: (v, r) => (v ? `${v} sh (${(r.unrealized_pnl ?? 0) >= 0 ? "+" : "−"}$${Math.abs(Math.round(r.unrealized_pnl ?? 0)).toLocaleString()})` : "flat"),
  },
  {
    key: "total_fees", title: "Fees", better: "low",
    help: "Commissions paid (slippage is already in the fill prices).", value: (r) => r.total_fees, format: money,
  },
  {
    key: "decision_errors", title: "Failed decisions", better: "low",
    help: "Decision days where decision-service failed (counted as a hold). Anything above 0 makes the result less trustworthy.",
    value: (r) => r.decision_errors ?? 0, format: (v) => num(v, 0),
  },
];

/** Direction-aware comparison: is a better than b? */
export const isBetter = (m: Metric, a: number, b: number) => (m.better === "low" ? a < b : a > b);

/** The best value of each metric across finished runs, for highlighting. Needs 2+ runs to mean anything. */
export function bestValues(results: Result[]): Map<string, number> {
  const best = new Map<string, number>();
  if (results.length < 2) return best;
  for (const m of METRICS) {
    if (!m.better) continue;
    const values = results.map(m.value).filter((v): v is number => v != null && !Number.isNaN(v));
    if (new Set(values).size < 2) continue; // all equal (e.g. no fees anywhere): nothing stands out
    best.set(m.key, values.reduce((a, b) => (isBetter(m, b, a) ? b : a)));
  }
  return best;
}
