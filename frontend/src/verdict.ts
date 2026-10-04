// A plain-words recommendation for one finished backtest: a short checklist of what a result needs before it
// is worth paper trading, and a grade from it. A backtest can at best earn "paper-trade it next": past results
// on one symbol and window never prove a strategy, so nothing here says "trade it with real money".

import dayjs from "dayjs";
import { FEW_TRADES } from "./checkAt";
import type { Result, RunConfig } from "./types";

/** "paper" = passed every check, "weak" = made money but missed a check, "no" = failed a must-have. */
export type Grade = "paper" | "weak" | "no";

export interface Check {
  ok: boolean;
  label: string;
  detail: string;
  mustHave?: boolean; // failing it means "don't trade this", whatever else passed
}

export interface Verdict {
  grade: Grade;
  title: string;
  summary: string;
  checks: Check[];
}

/** The bars a result has to clear. */
export const BARS = {
  minTrades: FEW_TRADES,
  minProfitFactor: 1.2, // every $1 lost has to bring back at least $1.20
  worstDrawdownPct: -20, // like the bots' default breaker
  minMonths: 6, // shorter tests see one market mood only
};

export const GRADE_LOOK: Record<Grade, { label: string; color: string; alert: "success" | "warning" | "error" }> = {
  paper: { label: "Worth paper trading", color: "green", alert: "success" },
  weak: { label: "Not yet", color: "gold", alert: "warning" },
  no: { label: "Not recommended", color: "red", alert: "error" },
};

const signed = (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;

export function judge(r: Result, cfg?: RunConfig | null): Verdict {
  const checks: Check[] = [];
  const n = r.num_trades;

  checks.push({
    mustHave: true,
    ok: n >= BARS.minTrades,
    label: "Enough trades",
    detail: `${n} closed trade${n === 1 ? "" : "s"} (at least ${BARS.minTrades} needed, 30+ is better; fewer and luck can explain everything)`,
  });

  const avg = r.avg_trade_pct ?? 0;
  checks.push({
    mustHave: true,
    ok: r.total_return_pct > 0 && avg > 0,
    label: "Made money",
    detail: `Return ${signed(r.total_return_pct)}, average trade ${signed(avg)}`,
  });

  // profit_factor null with winning trades = no losing trade yet (infinite)
  const pf = r.profit_factor ?? (n && r.win_rate_pct > 0 ? Infinity : 0);
  checks.push({
    ok: pf >= BARS.minProfitFactor,
    label: "Wins outweigh losses",
    detail:
      pf === Infinity
        ? "No losing trade yet"
        : `Each $1 lost brought back $${pf.toFixed(2)} (at least $${BARS.minProfitFactor.toFixed(2)} wanted)`,
  });

  const bhSharpe = r.buy_hold_sharpe;
  const smoother = r.sharpe != null && bhSharpe != null && r.sharpe > bhSharpe;
  checks.push({
    ok: !!r.beat_buy_hold || smoother,
    label: "Better than just holding",
    detail: r.beat_buy_hold
      ? `${signed(r.total_return_pct - r.buy_hold_return_pct)} ahead of buy & hold (${signed(r.buy_hold_return_pct)})`
      : smoother
        ? `Less return than buy & hold (${signed(r.buy_hold_return_pct)}), but a smoother ride (Sharpe ${r.sharpe} vs ${bhSharpe})`
        : `Buy & hold made ${signed(r.buy_hold_return_pct)} with a smoother or equal ride: simply holding was better`,
  });

  checks.push({
    ok: r.max_drawdown_pct > BARS.worstDrawdownPct && !r.breaker_tripped_on,
    label: "Losses stayed bearable",
    detail: r.breaker_tripped_on
      ? `The drawdown breaker tripped on ${r.breaker_tripped_on}`
      : `Worst drop ${r.max_drawdown_pct.toFixed(1)}% (shallower than ${BARS.worstDrawdownPct}% wanted)`,
  });

  if (cfg) {
    const months = dayjs(cfg.end).diff(dayjs(cfg.start), "month", true);
    checks.push({
      ok: months >= BARS.minMonths - 0.1,
      label: "Long enough test",
      detail: `${months.toFixed(0)} month${Math.round(months) === 1 ? "" : "s"} tested (at least ${BARS.minMonths} wanted)`,
    });
  }

  const errors = r.decision_errors ?? 0;
  checks.push({
    ok: errors === 0,
    label: "Clean run",
    detail: errors ? `${errors} decision day${errors === 1 ? "" : "s"} failed and counted as holds` : "Every decision day worked",
  });

  const failedMust = checks.filter((c) => c.mustHave && !c.ok);
  const failed = checks.filter((c) => !c.ok);
  if (failedMust.length) {
    return {
      grade: "no",
      title: GRADE_LOOK.no.label,
      summary:
        failedMust[0].label === "Enough trades"
          ? "Too few trades to judge: this result could be pure luck. Test a longer window or other symbols."
          : "This setup lost money here. Don't trade it; try other settings or another strategy.",
      checks,
    };
  }
  if (failed.length) {
    return {
      grade: "weak",
      title: GRADE_LOOK.weak.label,
      summary: `It made money, but missed ${failed.length} check${failed.length === 1 ? "" : "s"} (${failed
        .map((c) => c.label.toLowerCase())
        .join(", ")}). Improve the settings or compare other strategies before paper trading.`,
      checks,
    };
  }
  return {
    grade: "paper",
    title: GRADE_LOOK.paper.label,
    summary:
      "Passed every check on this symbol and window. That's a reason to keep testing, not proof: try another period " +
      "or a similar symbol, then paper-trade it before risking real money.",
    checks,
  };
}
