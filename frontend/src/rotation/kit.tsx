// The momentum rotation's part of the shared Test results grid and saved-setups picker (studies/).

import type { StrategyKit } from "../studies/kit";
import { tradingApi } from "../trading/api";
import type { RotationPresetConfig } from "./types";

/** The backtest form's defaults: a setup saved without a rule gets these. */
export const ROTATION_DEFAULTS = { top_n: 3, lookback_months: 12, skip_months: 1, abs_filter: true, fractional: true };

/** "12-1": the momentum months and those it leaves out */
export const momentumText = (c: Pick<RotationPresetConfig, "lookback_months" | "skip_months">) =>
  c.skip_months ? `${c.lookback_months}-${c.skip_months}` : `${c.lookback_months}-month`;

export const ROTATION_KIT: StrategyKit<RotationPresetConfig> = {
  strategy: "rotation",
  // The rules that differ between the tests of a sweep, as grid columns (trading-service studies.ROTATION_RULES)
  rules: [
    { key: "top", title: "Hold", tip: "Hold the strongest this many, in equal parts", value: (c) => c.top_n, text: (c) => `${c.top_n}` },
    { key: "months", title: "Momentum", tip: "Ranked by the return over this many months", value: (c) => c.lookback_months, text: (c) => `${c.lookback_months} mo` },
    { key: "skip", title: "Skip", tip: "The latest months left out of the momentum (the classic 12-1 skips one: last month's winners tend to slip back)",
      value: (c) => c.skip_months ?? 0, text: (c) => (c.skip_months ? `${c.skip_months} mo` : "—") },
    { key: "rising", title: "Rising", tip: "Only hold what rose: a slot without one waits in cash (some protection in a crash)",
      value: (c) => (c.abs_filter ? 1 : 0), text: (c) => (c.abs_filter ? "rising only" : "any") },
    { key: "cash", title: "Capital", tip: "Starting capital (fractional shares on)", value: (c) => c.initial_cash ?? 0, text: (c) => `$${c.initial_cash ?? "—"}` },
  ],
  describe: (c) => (
    <>
      At each month's last close, rank the {c.symbols.length} symbols by their return over the past {c.lookback_months} months
      {c.skip_months ? `, leaving out the latest ${c.skip_months === 1 ? "month" : `${c.skip_months} months`}` : ""}, and hold the strongest {c.top_n} in
      equal parts{c.abs_filter ? ", only those that rose (a slot without one waits in cash)" : ", whether they rose or not"}; sell the rest.{" "}
      {c.initial_cash ? `About $${Math.round(c.initial_cash / c.top_n)} each at the start, ` : ""}{c.fractional ? "fractional shares" : "whole shares"}.
    </>
  ),
  /** "top 3 · 12-1 momentum · rising only · 11 symbols" */
  summary: (c) =>
    [`top ${c.top_n}`, `${momentumText(c)} momentum`, c.abs_filter ? "rising only" : "any", ...(c.fractional ? [] : ["whole shares"]),
      `${c.symbols?.length ?? 0} symbols`].join(" · "),
  canon: (c) => {
    const r = { ...ROTATION_DEFAULTS, ...c };
    return JSON.stringify([
      r.top_n, r.lookback_months, r.skip_months, r.abs_filter, r.fractional, c.symbols ?? [], c.initial_cash ?? null,
      c.period_months ? [c.period_months, !!c.exam] : [c.practice ?? null, c.exam ?? null],
    ]);
  },
  complete: (c) => ({ ...ROTATION_DEFAULTS, ...c }),
  presets: { list: tradingApi.rotationPresets, save: tradingApi.saveRotationPreset, remove: tradingApi.deleteRotationPreset },
  keeps: "the rules, the universe, the capital and the periods",
  quarters: {
    title: "Years",
    tip: "Separate 1-year tests back to back: how many made money, and the worst one",
    short: "Y",
  },
  stops: false,
  listWord: "universe",
  periodTips: {
    early: (n) => `The ${n} months before the practice: the 2018 sell-off and the 2020 crash.`,
    bear: () => "2022, the bear year: the S&P 500 fell about 19%.",
  },
};
