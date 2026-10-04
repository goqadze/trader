import { strategyShortName, type StrategyId } from "../strategies";
import { familyOf } from "../symbols";
import type { ScanCell, ScanRunView } from "./types";

const passed = (r: ScanRunView | null) => r?.verdict?.grade === "paper";

/** Strategies that passed on more than one symbol: the "does it hold up elsewhere?" check, in one line each, e.g.
 *  "Momentum 3 of 18 (twin ETFs counted once)". Twin ETFs (SPY, VOO, IVV...) count once: passing on all three is one
 *  result, not three. With an exam, only combinations that passed both count. */
export function consistency(cells: ScanCell[], symbols: string[], hasExam: boolean): string[] {
  const families = new Set(symbols.map(familyOf)).size;
  const by = new Map<StrategyId, Set<string>>();
  for (const c of cells)
    if (passed(c.practice) && (!hasExam || passed(c.exam))) by.set(c.strategy, (by.get(c.strategy) ?? new Set()).add(familyOf(c.symbol)));
  return [...by.entries()]
    .sort((a, b) => b[1].size - a[1].size)
    .map(([id, fams]) => `${strategyShortName(id)} ${fams.size} of ${families}${families < symbols.length ? " (twin ETFs counted once)" : ""}`);
}
