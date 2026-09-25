// "Check at" for backtests: when a decision day decides, like a trading bot. After the open replays 10:00 from
// 30-minute prices (decision-service gets them from Alpaca, years back).

import dayjs, { type Dayjs } from "dayjs";
import type { DecideAt } from "./trading/types";

export const CHECK_AT_OPTIONS: { value: DecideAt; label: string }[] = [
  { value: "close", label: "Before the close · 15:30" },
  { value: "open", label: "After the open · 10:00" },
  { value: "both", label: "Both" },
];

/** "before the close (15:30)", for sentences. */
export const checkAtText = (d: DecideAt | undefined) =>
  d === "open" ? "after the open (10:00)" : d === "both" ? "at 10:00 and 15:30" : "before the close (15:30)";

/** The default backtest window: the last year. Fewer days mean fewer trades, too few to judge a strategy. */
export const defaultRange = (): [Dayjs, Dayjs] => [dayjs().subtract(1, "year"), dayjs()];

/** A result built on this few closed trades says little: luck can explain it. */
export const FEW_TRADES = 10;
