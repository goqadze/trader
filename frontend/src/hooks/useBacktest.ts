import { useCallback, useRef, useState } from "react";
import { createRun, openRunSocket } from "../api";
import type { BacktestState, RunConfig, RunEvent } from "../types";

// Initial UI state for one backtest run.
const INITIAL: BacktestState = {
  status: "idle",
  symbol: "",
  total: 0,
  i: 0,
  equity: null,
  returnPct: null,
  position: 0,
  trades: 0,
  chart: [],
  log: [],
  result: null,
  error: null,
  config: null,
};

/**
 * Manages a backtest: POST /runs, then consume the WebSocket event stream and
 * fold each event into React state the components render.
 */
export function useBacktest() {
  const [state, setState] = useState<BacktestState>(INITIAL);
  const wsRef = useRef<WebSocket | null>(null);
  const metaRef = useRef({ initialCash: 0, bhShares: 0 }); // benchmark math, kept out of render state

  const handle = useCallback((ev: RunEvent) => {
    if (ev.type === "start") {
      metaRef.current = { initialCash: ev.initial_cash, bhShares: ev.initial_cash / ev.first_price };
      setState((s) => ({ ...s, status: "running", symbol: ev.symbol, total: ev.total }));
    } else if (ev.type === "step") {
      const { initialCash, bhShares } = metaRef.current;
      setState((s) => {
        // Every day feeds the chart (smooth curve); buy & hold is derived from the same price.
        const chart = [
          ...s.chart,
          { date: ev.date, strategy: Math.round(ev.equity), buyhold: Math.round(bhShares * ev.price) },
        ];
        // Only decision days (BUY/SELL/HOLD, uppercase) go in the log; "hold" carry-days don't.
        const isDecision = ev.action === "BUY" || ev.action === "SELL" || ev.action === "HOLD";
        const log = isDecision
          ? [
              {
                key: ev.seq,
                date: ev.date,
                action: ev.action,
                confidence: ev.confidence,
                price: ev.price,
                equity: ev.equity,
                sentiment: ev.sentiment,
                reasoning: ev.reasoning,
              },
              ...s.log,
            ].slice(0, 200)
          : s.log;
        return {
          ...s,
          i: ev.i,
          equity: ev.equity,
          returnPct: (ev.equity / initialCash - 1) * 100,
          position: ev.position,
          chart,
          log,
        };
      });
    } else if (ev.type === "trade") {
      setState((s) => ({ ...s, trades: s.trades + 1 }));
    } else if (ev.type === "done") {
      setState((s) => ({ ...s, status: "done", result: ev.result, trades: ev.result.num_trades, returnPct: ev.result.total_return_pct }));
    } else if (ev.type === "error") {
      setState((s) => ({ ...s, status: "error", error: ev.message }));
    }
  }, []);

  const start = useCallback(
    async (cfg: RunConfig) => {
      wsRef.current?.close();
      setState({ ...INITIAL, status: "starting", symbol: cfg.symbol, config: cfg });
      let run: { run_id: string };
      try {
        run = await createRun(cfg);
      } catch (e) {
        setState((s) => ({ ...s, status: "error", error: e instanceof Error ? e.message : String(e) }));
        return;
      }
      wsRef.current = openRunSocket(run.run_id, handle);
    },
    [handle]
  );

  return { state, start };
}
