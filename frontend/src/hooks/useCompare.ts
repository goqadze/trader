import { useCallback, useEffect, useRef, useState } from "react";
import { createRun, openRunSocket } from "../api";
import type { StrategyId } from "../strategies";
import type { BacktestState, RunConfig } from "../types";
import { INITIAL, applyEvent } from "./useBacktest";

/** One backtest per strategy, all on the same symbol and window. */
export interface CompareState {
  order: StrategyId[]; // the strategies in the order they were picked
  runs: Partial<Record<StrategyId, BacktestState>>;
}

/**
 * Runs several strategies side by side: one POST /runs + WebSocket per strategy, each folded into its own
 * BacktestState with the same reducer the single backtest uses. backtest-service runs a few at a time and
 * queues the rest, which show as "starting" until their first event.
 */
export function useCompare() {
  const [state, setState] = useState<CompareState>({ order: [], runs: {} });
  const socketsRef = useRef<WebSocket[]>([]);
  const genRef = useRef(0); // bumps on every start, so a slow reply from the previous comparison is dropped

  const closeAll = () => {
    socketsRef.current.forEach((ws) => ws.close());
    socketsRef.current = [];
  };
  useEffect(() => closeAll, []); // leaving the page stops listening (the runs finish on the server anyway)

  const update = (id: StrategyId, f: (s: BacktestState) => BacktestState) =>
    setState((c) => ({ ...c, runs: { ...c.runs, [id]: f(c.runs[id] ?? INITIAL) } }));

  const start = useCallback(async (configs: RunConfig[]) => {
    closeAll();
    const gen = ++genRef.current;
    setState({
      order: configs.map((c) => c.strategy),
      runs: Object.fromEntries(configs.map((c) => [c.strategy, { ...INITIAL, status: "starting", symbol: c.symbol, config: c }])),
    });
    await Promise.all(
      configs.map(async (cfg) => {
        try {
          const run = await createRun(cfg);
          if (gen !== genRef.current) return;
          socketsRef.current.push(openRunSocket(run.run_id, (ev) => update(cfg.strategy, (s) => applyEvent(s, ev))));
        } catch (e) {
          if (gen === genRef.current) update(cfg.strategy, (s) => ({ ...s, status: "error", error: e instanceof Error ? e.message : String(e) }));
        }
      })
    );
  }, []);

  return { state, start };
}
