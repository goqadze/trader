import type { StrategyId } from "../strategies";
import { familyOf } from "../symbols";
import type { ScanCell, ScanRunView, ScanView } from "./types";

/** One strategy's closed trades on every symbol of a scan, added up. */
export interface Pool {
  symbols: number; // symbols with a finished run
  trades: number;
  winRate: number | null; // %
  profitFactor: number | null; // $ won / $ lost; Infinity = no losing trade; null = no trades
  avgTrade: number | null; // % per trade
  profitable: number; // symbols that made money
  breakers: number; // symbols whose drawdown breaker tripped
}

export interface PooledRow {
  strategy: StrategyId;
  practice: Pool;
  exam: Pool | null; // only when every combination sat the exam (else the exam pool is only practice's winners)
  twinsSkipped: number; // twin ETFs left out so one index isn't counted several times
}

function pool(runs: ScanRunView[]): Pool {
  const done = runs.filter((r) => r.summary);
  let trades = 0, wins = 0, won = 0, lost = 0, pctSum = 0, profitable = 0, breakers = 0;
  for (const r of done) {
    const s = r.summary!;
    trades += s.num_trades;
    wins += (s.win_rate_pct / 100) * s.num_trades;
    won += s.gross_win ?? 0;
    lost += s.gross_loss ?? 0;
    pctSum += (s.avg_trade_pct ?? 0) * s.num_trades;
    profitable += s.total_return_pct > 0 ? 1 : 0;
    breakers += s.breaker_tripped_on ? 1 : 0;
  }
  return {
    symbols: done.length,
    trades,
    winRate: trades ? (wins / trades) * 100 : null,
    profitFactor: !trades ? null : lost > 0 ? won / lost : won > 0 ? Infinity : null,
    avgTrade: trades ? pctSum / trades : null,
    profitable,
    breakers,
  };
}

/** Per strategy, its trades on all of the scan's symbols together: one symbol can be lucky, hundreds of trades
 *  across symbols show whether the rule itself has an edge. Of twin ETFs (SPY, VOO, IVV...) only the first counts. */
export function pooled(scan: ScanView): PooledRow[] {
  const first = new Map<string, string>(); // family -> the first of its symbols in the scan
  for (const s of scan.config.symbols) if (!first.has(familyOf(s))) first.set(familyOf(s), s);
  const counted = (c: ScanCell) => first.get(familyOf(c.symbol)) === c.symbol;
  const strategies = [...new Set(scan.cells.map((c) => c.strategy))];
  return strategies.map((strategy) => {
    const cells = scan.cells.filter((c) => c.strategy === strategy);
    const kept = cells.filter(counted);
    return {
      strategy,
      practice: pool(kept.flatMap((c) => (c.practice ? [c.practice] : []))),
      exam: scan.config.exam && scan.config.exam_all ? pool(kept.flatMap((c) => (c.exam ? [c.exam] : []))) : null,
      twinsSkipped: cells.length - kept.length,
    };
  });
}
