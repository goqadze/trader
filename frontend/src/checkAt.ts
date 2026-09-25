// "Check at" for backtests: when a decision day decides, like a trading bot. Before the close works for any
// window; after the open replays 10:00 from Yahoo's 30-minute prices, which only go back 60 days.

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

/** backtest-service allows 59 days back in New York time; a day less here covers the time-zone difference. */
const INTRADAY_DAYS = 58;

/** The earliest start a backtest can use when it checks after the open. */
export const earliestIntradayStart = (): Dayjs => dayjs().subtract(INTRADAY_DAYS, "day").startOf("day");

/** The default backtest window: about 2 months, the most that still allows every check time. */
export const defaultRange = (): [Dayjs, Dayjs] => [earliestIntradayStart(), dayjs()];

export const needsIntraday = (d: DecideAt | undefined) => d === "open" || d === "both";

/** A window starting too early for 30-minute prices, moved to the earliest start that has them. */
export function clampToIntraday(range: [Dayjs, Dayjs] | undefined): [Dayjs, Dayjs] | undefined {
  const first = earliestIntradayStart();
  return range && range[0].isBefore(first, "day") ? [first, range[1].isBefore(first, "day") ? dayjs() : range[1]] : range;
}
