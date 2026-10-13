// The dip buyer's part of the shared Test results grid and saved-setups picker (studies/).

import type { StrategyKit } from "../studies/kit";
import { tradingApi } from "../trading/api";
import { type DipPresetConfig, fallText, pickRules, sellText } from "./types";

const pct = (f: number) => `${+(f * 100).toFixed(2)}%`;

export const DIP_KIT: StrategyKit<DipPresetConfig> = {
  strategy: "dip",
  // The rules that differ between the tests of a sweep, as grid columns (trading-service studies.DIP_RULES)
  rules: [
    { key: "drop", title: "Fall", tip: "Buy after a fall of this much (or this many times the symbol's usual daily move) ...",
      value: (c) => (c.drop_mode === "volatility" ? 10 + c.drop_atr : c.drop_pct),
      text: (c) => (c.drop_mode === "volatility" ? `${+c.drop_atr.toFixed(2)}× move` : pct(c.drop_pct)) },
    { key: "lookback", title: "In", tip: "... under the highest close of the last this many trading days", value: (c) => c.lookback, text: (c) => `${c.lookback}d` },
    { key: "turn", title: "Turn", tip: "Wait for the turn: buy once it is back up this much from its low (off: buy at once)", value: (c) => (c.rebound ? c.rebound_pct : 0), text: (c) => (c.rebound ? pct(c.rebound_pct) : "off") },
    { key: "rise", title: "Sell", tip: "Sell when it is up this much from the buy", value: (c) => (c.target_mode === "percent" ? c.rise_pct : 0), text: (c) => (c.target_mode === "percent" ? `+${pct(c.rise_pct)}` : "back") },
    { key: "stop", title: "Stop", tip: "Stop-loss under the buy: sold and blacklisted", value: (c) => c.stop_pct, text: (c) => pct(c.stop_pct) },
    { key: "reenable", title: "Blacklist", tip: "A stopped-out symbol is blacklisted this many trading days", value: (c) => c.reenable_days ?? 0, text: (c) => `${c.reenable_days ?? 0}d` },
    { key: "hold", title: "Max hold", tip: "Sell anyway after this many trading days (— = never)", value: (c) => c.max_hold_days ?? 0, text: (c) => (c.max_hold_days ? `${c.max_hold_days}d` : "—") },
    { key: "trend", title: "Trend", tip: "Only buy dips of symbols in an uptrend (50-day average above the 200-day)", value: (c) => (c.trend_filter ? 1 : 0), text: (c) => (c.trend_filter ? "up only" : "—") },
    { key: "slots", title: "Slots", tip: "Positions at once: each buy gets 1/slots of the equity", value: (c) => c.max_positions, text: (c) => `${c.max_positions}` },
    { key: "cash", title: "Capital", tip: "Starting capital (fractional shares on)", value: (c) => c.initial_cash ?? 0, text: (c) => `$${c.initial_cash ?? "—"}` },
  ],
  describe: (c) => {
    const r = pickRules(c);
    return (
      <>
        Every {r.interval === "1d" ? "day" : r.interval}: buy {fallText(r)}; sell {sellText(r)}; stop-loss {pct(r.stop_pct)} under the buy, then the
        symbol is blacklisted for {c.reenable_days ?? 0} trading days. {r.max_positions} positions at once, each 1/{r.max_positions} of the equity
        {c.initial_cash ? ` (about $${Math.round(c.initial_cash / r.max_positions)} each at the start)` : ""}, {r.fractional ? "fractional shares" : "whole shares"}.
      </>
    );
  },
  /** "7% in 60d · turn 1% · stop 10% · every 15m · 12 symbols" */
  summary: (c) => {
    const r = pickRules(c);
    return [
      `${r.drop_mode === "volatility" ? `${+r.drop_atr.toFixed(2)}× move` : `${pct(r.drop_pct)}${r.drop_mode === "price" ? " by price" : ""}`} in ${r.lookback}${r.lookback_unit === "hours" ? "h" : "d"}`,
      r.rebound ? `turn ${pct(r.rebound_pct)}` : "no turn",
      `stop ${pct(r.stop_pct)}`,
      r.interval === "1d" ? "daily" : `every ${r.interval}`,
      ...(r.fractional ? ["fractional"] : []),
      `${c.symbols?.length ?? 0} symbols`,
    ].join(" · ");
  },
  canon: (c) =>
    JSON.stringify([
      pickRules(c), c.symbols ?? [], c.reenable_days ?? 0, c.initial_cash ?? null,
      c.period_months ? [c.period_months, !!c.exam] : [c.practice ?? null, c.exam ?? null],
    ]),
  complete: (c) => ({ ...c, ...pickRules(c) }),
  presets: { list: tradingApi.dipPresets, save: tradingApi.saveDipPreset, remove: tradingApi.deleteDipPreset },
  keeps: "the rules, the watchlist, the capital, the blacklist and the periods",
  quarters: {
    title: "Quarters",
    tip: "Twelve separate 3-month tests, Oct 2023 to Oct 2026: how many made money, and the worst one. Run for the best setups only",
    short: "Q",
  },
  stops: true,
  listWord: "watchlist",
};
