import { useCallback, useRef, useState } from "react";
import { createRun, openRunSocket } from "../api";
import type { BacktestState, RunConfig, RunEvent } from "../types";

// Initial UI state for one backtest run.
export const INITIAL: BacktestState = {
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
  initialCash: 0,
  bhShares: 0,
};

// When a slot decides, New York time (backtest-service's OPEN_SLOT; the close decision uses the day's close)
const SLOT_TIMES = { open: "10:00", close: "15:30" } as const;

/** Fold one WebSocket event into a run's UI state. Pure, so the strategy comparison reuses it per run. */
export function applyEvent(s: BacktestState, ev: RunEvent): BacktestState {
  if (ev.type === "start") {
    return { ...s, status: "running", symbol: ev.symbol, total: ev.total, initialCash: ev.initial_cash, bhShares: ev.initial_cash / ev.first_price };
  }
  if (ev.type === "step") {
    // Every day feeds the chart (smooth curve); buy & hold is derived from the same price.
    const chart = [...s.chart, { date: ev.date, strategy: Math.round(ev.equity), buyhold: Math.round(s.bhShares * ev.price) }];
    // Only decisions go in the log (newest first), not the carry days in between
    const showTime = s.config?.decide_at != null && s.config.decide_at !== "close";
    const rows = (ev.decisions ?? []).map((d, k) => ({
      key: ev.seq * 2 + k,
      date: ev.date,
      time: showTime ? SLOT_TIMES[d.slot] : undefined,
      action: d.action,
      confidence: d.confidence,
      price: d.price,
      equity: d.equity,
      sentiment: d.sentiment,
      reasoning: d.reasoning,
    }));
    const log = rows.length ? [...rows.reverse(), ...s.log].slice(0, 200) : s.log;
    return { ...s, i: ev.i, equity: ev.equity, returnPct: (ev.equity / s.initialCash - 1) * 100, position: ev.position, chart, log };
  }
  if (ev.type === "trade") return { ...s, trades: s.trades + 1 };
  if (ev.type === "done") return { ...s, status: "done", result: ev.result, trades: ev.result.num_trades, returnPct: ev.result.total_return_pct };
  if (ev.type === "error") return { ...s, status: "error", error: ev.message };
  return s;
}

/**
 * Manages a backtest: POST /runs, then consume the WebSocket event stream and
 * fold each event into React state the components render.
 */
export function useBacktest() {
  const [state, setState] = useState<BacktestState>(INITIAL);
  const wsRef = useRef<WebSocket | null>(null);

  const handle = useCallback((ev: RunEvent) => setState((s) => applyEvent(s, ev)), []);

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
