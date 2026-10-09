// "Test the last n months": the backtest forms' quick choice of practice and exam windows counted back from today.

import dayjs, { type Dayjs } from "dayjs";

export type Range = [Dayjs, Dayjs];

export const MONTHS = [1, 2, 3, 4, 6, 9, 12, 18, 24, 36];

export const monthsLabel = (n: number) => (n % 12 === 0 ? `${n / 12} year${n > 12 ? "s" : ""}` : `${n} month${n > 1 ? "s" : ""}`);

/** "the last month", "the last year", "the last 3 months" */
export const spanWords = (n: number) => (n === 1 ? "month" : n === 12 ? "year" : monthsLabel(n));

/** The periods for "the last n months": the exam is the last n months up to today and the practice the n months
 *  before it; without an exam, the practice is the last n months. */
export function lastMonths(n: number, exam: boolean, today = dayjs()): { practice: Range; exam?: Range } {
  const split = today.subtract(n, "month");
  return exam ? { practice: [today.subtract(2 * n, "month"), split.subtract(1, "day")], exam: [split, today] } : { practice: [split, today] };
}

/** The "Test the last" select's options: each span, then "Custom dates" (0). */
export const periodOptions = (months: number[] = MONTHS) => [...months.map((n) => ({ value: n, label: monthsLabel(n) })), { value: 0, label: "Custom dates" }];

/** The line under the select: which windows that span means. */
export const periodExtra = (n: number | undefined, examOn: boolean) =>
  n ? (examOn ? `Exam: the last ${spanWords(n)}. Practice: the ${spanWords(n)} before.` : `Practice: the last ${spanWords(n)}.`) : undefined;
