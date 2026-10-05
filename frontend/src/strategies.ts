// The decision strategies decision-service can run, for the pickers on the backtest and bot forms.
// Mirrors decision-service/app/strategies.py (its GET /strategies returns the same catalog); keep the ids
// in sync with it, trading-service/app/schemas.py and backtest-service/app/models.py. The intraday ones
// (decision-service/app/intraday_strategies.py) only backtest: live bots can't run them yet.

export type StrategyId =
  | "sma_rsi"
  | "trend_following"
  | "momentum"
  | "breakout"
  | "mean_reversion"
  | "range_trading"
  | "ma_pullback"
  | "reversal"
  | "gap_and_go"
  | "news_catalyst"
  | "fibonacci"
  | "orb"
  | "ict_sweep_fvg"
  | "ict_amd";

export interface StrategyInfo {
  id: StrategyId;
  name: string;
  style: "trend" | "momentum" | "breakout" | "mean reversion" | "event" | "intraday";
  summary: string;
  buy: string;
  sell: string;
  bestFor: string;
  /** Starting points for the knobs that matter most for this style: backtest before trusting them. */
  suggested: { rebalance_days: number; stop_pct: number; target_pct: number; decide_at?: "close" | "open" | "both" };
  /** Intraday strategies: their settings instead (their stop and target come from each day's setup). */
  intraday?: IntradaySettings;
}

export interface IntradaySettings {
  risk_pct: number; // what one trade may lose, as a fraction of equity
  sides: "long" | "both";
  slippage_pct: number; // per market fill: liquid ETFs trade with a one-cent spread
}

export const INTRADAY_DEFAULTS: IntradaySettings = { risk_pct: 0.01, sides: "both", slippage_pct: 0.0001 };

/** The style groups in display order (pickers, comparison and scan checklists). */
export const STYLES = ["trend", "momentum", "breakout", "mean reversion", "event", "intraday"] as const;

export const DEFAULT_STRATEGY: StrategyId = "sma_rsi";

export const STRATEGIES: StrategyInfo[] = [
  {
    id: "sma_rsi", name: "SMA trend + RSI (simple)", style: "trend",
    summary: "The original rule: be long while the 20-day average is above the 50-day, unless RSI says overbought.",
    buy: "SMA20 > SMA50 and RSI14 < 70", sell: "SMA20 ≤ SMA50 and RSI14 > 30",
    bestFor: "steady trends; the baseline to beat",
    suggested: { rebalance_days: 5, stop_pct: 0.04, target_pct: 0.08 },
  },
  {
    id: "trend_following", name: "Trend following (golden cross + ADX)", style: "trend",
    summary: "Ride long-term uptrends: SMA50 above SMA200 with ADX confirming a real trend; exit on the death cross.",
    buy: "SMA50 > SMA200, price > SMA50, ADX ≥ 20 with +DI > −DI", sell: "SMA50 < SMA200, or price < SMA200 with −DI > +DI",
    bestFor: "strong multi-month trends; the death cross exits, so the take-profit is set far away to let winners run",
    suggested: { rebalance_days: 5, stop_pct: 0.08, target_pct: 1.0 },
  },
  {
    id: "momentum", name: "Momentum (MACD + RSI)", style: "momentum",
    summary: "Buy strength that is still building: positive 3-month return, MACD rising above its signal, RSI 50–70.",
    buy: "3-month return > 0, price > SMA50, MACD > signal and rising, 50 ≤ RSI < 70",
    sell: "MACD < signal with RSI < 50, or price < SMA50 with MACD < 0",
    bestFor: "trending markets and leaders; avoids chasing RSI > 70",
    suggested: { rebalance_days: 5, stop_pct: 0.06, target_pct: 0.15 },
  },
  {
    id: "breakout", name: "Breakout (20-day high + volume)", style: "breakout",
    summary: "Buy a close above the 20-day high on 1.5×+ volume; exit on a close below the 10-day low (Turtle rule).",
    buy: "close > prior 20-day high, volume ≥ 1.5× average, close in the upper half of the day", sell: "close < prior 10-day low",
    bestFor: "stocks leaving a tight consolidation; check daily so the breakout day isn't missed",
    suggested: { rebalance_days: 1, stop_pct: 0.05, target_pct: 0.15 },
  },
  {
    id: "mean_reversion", name: "Mean reversion (Bollinger + RSI)", style: "mean reversion",
    summary: "Buy an oversold dip below the lower Bollinger Band once a candle closes back inside; sell back at the mean.",
    buy: "low < lower band with RSI < 30 in the last 3 days, then an up close back inside (not in a strong downtrend)",
    sell: "close ≥ middle band (the mean)",
    bestFor: "range-bound stocks and dips inside long-term uptrends; check daily",
    suggested: { rebalance_days: 1, stop_pct: 0.05, target_pct: 0.08 },
  },
  {
    id: "range_trading", name: "Range trading (support & resistance)", style: "mean reversion",
    summary: "In a sideways market (ADX < 20), buy bounces at support and sell near resistance; exit if support breaks.",
    buy: "ADX < 20, both edges of the 40-day range tested twice, price in the bottom 25% with an up candle",
    sell: "price in the top 25% of the range, or a close below support",
    bestFor: "sideways, choppy stocks",
    suggested: { rebalance_days: 1, stop_pct: 0.04, target_pct: 0.08 },
  },
  {
    id: "ma_pullback", name: "Swing: pullback to the 20 EMA", style: "trend",
    summary: "Buy the dip inside an uptrend: EMA20 above a rising EMA50, price pulls back to EMA20 and is rejected upward.",
    buy: "EMA20 > rising EMA50, low touched EMA20 today/yesterday, up close back above it", sell: "EMA20 < EMA50 or close < EMA50",
    bestFor: "healthy uptrends with regular pullbacks; holds days to weeks",
    suggested: { rebalance_days: 1, stop_pct: 0.05, target_pct: 0.12 },
  },
  {
    id: "reversal", name: "Reversal (RSI divergence)", style: "mean reversion",
    summary: "Catch a turn: price makes a lower low but RSI a higher low (selling is exhausting), confirmed by an up candle.",
    buy: "bullish RSI divergence (first low RSI < 35, newest low ≤ 5 bars ago), up close above yesterday's close",
    sell: "bearish RSI divergence at the highs plus a down candle",
    bestFor: "oversold stocks after a long decline; contrarian, lower win rate",
    suggested: { rebalance_days: 1, stop_pct: 0.05, target_pct: 0.1 },
  },
  {
    id: "gap_and_go", name: "Gap and go", style: "event",
    summary: "Buy a big opening gap up that holds all day on heavy volume, better still with a news catalyst behind it.",
    buy: "open ≥ 2% (and ≥ ½ ATR) above yesterday's close, never back to it, close ≥ open, volume ≥ 1.5×",
    sell: "a gap down that stays below the open and never recovers",
    bestFor: "earnings and news days; check daily, after the open",
    suggested: { rebalance_days: 1, stop_pct: 0.03, target_pct: 0.06, decide_at: "open" },
  },
  {
    id: "news_catalyst", name: "News catalyst (RAG-driven)", style: "event",
    summary: "Trade what the news RAG finds when the market agrees: bullish news on an up day buys, bearish on a down day sells.",
    buy: "news bullish and price up today; 0.55 base, +0.10 fresh catalyst (earnings, upgrade, deal…), +0.10 volume ≥ 1.5×",
    sell: "news bearish and price down today (same scoring)",
    bestFor: "event-driven stocks; needs news keys; check daily",
    suggested: { rebalance_days: 1, stop_pct: 0.04, target_pct: 0.1, decide_at: "both" },
  },
  {
    id: "fibonacci", name: "Fibonacci retracement (golden zone)", style: "trend",
    summary: "After a strong up-move (8%+), buy the pullback into the 50–61.8% golden zone once a bullish candle holds it.",
    buy: "low reaches the 50% level, close ≥ 61.8% level on an up candle, 78.6% never broken", sell: "close below the 78.6% level",
    bestFor: "trending stocks after a sharp run-up",
    suggested: { rebalance_days: 1, stop_pct: 0.05, target_pct: 0.12 },
  },
  {
    id: "orb", name: "Opening range breakout (5-min ORB)", style: "intraday",
    summary: "Trade the direction of the first 5-minute candle from 9:35: long if it closed up, short if down; stop at its other end, target 10× the risk, out by 15:55.",
    buy: "first 5-minute candle closes up: buy at 9:35, stop at its low", sell: "it closes down: short at 9:35, stop at its high (needs long and short)",
    bestFor: "liquid index ETFs (QQQ, SPY); the published intraday baseline (Zarattini & Aziz, 2023)",
    suggested: { rebalance_days: 1, stop_pct: 0.01, target_pct: 0.1 }, intraday: INTRADAY_DEFAULTS,
  },
  {
    id: "ict_sweep_fvg", name: "ICT: sweep → shift → FVG", style: "intraday",
    summary: "9:30–11:00 New York: price sweeps yesterday's or the opening range's low, a strong candle breaks the last swing high and leaves a fair value gap in discount; buy back into the gap's middle. Shorts mirror it.",
    buy: "sweep of a low + close back above, then the first close above the swing high leaving a bullish FVG below 50% of the move: limit at the gap's middle, stop under the sweep, target the nearest high paying ≥ 2× the risk",
    sell: "the mirror: sweep of a high, break of the swing low, bearish FVG in premium (needs long and short)",
    bestFor: "QQQ and SPY (stand-ins for NQ and ES); ICT made into exact rules: unproven, which is what the test is for",
    suggested: { rebalance_days: 1, stop_pct: 0.01, target_pct: 0.02 }, intraday: INTRADAY_DEFAULTS,
  },
  {
    id: "ict_amd", name: "ICT Power of 3 (accumulation → manipulation → distribution)", style: "intraday",
    summary: "Accumulation: the pre-market range. Manipulation: in the first hour price runs one side of it and closes back inside. Distribution: a shift through the swing high with a fair value gap in discount; buy back into the gap, target the other side of the range. Only longs when the day opens in the lower half of yesterday's range, only shorts in the upper half.",
    buy: "opened in discount, swept the pre-market low by 10:30 and closed back above, then closed above the swing high: limit at the move's first FVG (in discount), stop under the sweep, target the pre-market high or yesterday's high (the nearer paying ≥ 2× the risk)",
    sell: "the mirror: opened in premium, swept the pre-market high, broke the swing low, FVG in premium",
    bestFor: "MNQ / MES (or QQQ / SPY): about one setup a month per index in 2023–26, so it trades rarely; ICT made into exact rules, unproven",
    suggested: { rebalance_days: 1, stop_pct: 0.01, target_pct: 0.02 }, intraday: INTRADAY_DEFAULTS,
  },
];

const BY_ID = new Map(STRATEGIES.map((s) => [s.id, s]));

export const strategyInfo = (id: string): StrategyInfo | undefined => BY_ID.get(id as StrategyId);
export const strategyName = (id: string) => strategyInfo(id)?.name ?? id;

export const isIntraday = (id: string | undefined) => strategyInfo(id ?? "")?.style === "intraday";

/** Intraday strategies with a limit-or-market choice of entry (ORB always enters at market). */
export const hasEntryChoice = (id: string | undefined) => id === "ict_sweep_fvg" || id === "ict_amd";

export const HTF_OPTIONS = [
  { value: "off", label: "Off" },
  { value: "1h", label: "With the 1-hour trend" },
  { value: "4h", label: "With the 4-hour trend" },
  { value: "1d", label: "With the daily trend" },
];

export const ENTRY_OPTIONS = [
  { value: "limit", label: "Limit at the gap's middle (waits for a pullback)" },
  { value: "market", label: "At market as soon as the setup completes" },
];

/** Select options grouped by style, for antd's <Select options>. */
export const STRATEGY_OPTIONS = STYLES.map((style) => ({
  label: style,
  options: STRATEGIES.filter((s) => s.style === style).map((s) => ({ value: s.id, label: s.name })),
}));

/** The bot form's options: live bots can't run the intraday strategies yet. */
export const BOT_STRATEGY_OPTIONS = STRATEGY_OPTIONS.filter((g) => g.label !== "intraday");

/** The name without its parenthesized detail ("Breakout (20-day high + volume)" -> "Breakout"), for tabs and tables. */
export const strategyShortName = (id: string) => strategyName(id).replace(/\s*\(.*\)\s*$/, "");
