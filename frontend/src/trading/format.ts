// Formatting helpers shared by the trading pages. Market times are shown in New York time,
// because that's the clock the exchange (and the bots' schedule) runs on.

// Values within half a cent of zero print as $0.00 (float dust would otherwise show "-$0.00")
export const usd = (v: number | null | undefined, digits = 2) =>
  v == null
    ? "—"
    : (Math.abs(v) < 0.005 ? 0 : v).toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });

export const pct = (v: number | null | undefined, digits = 2) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`);

/** 0.04 -> "4%" (parameters are stored as fractions) */
export const frac = (v: number) => `${+(v * 100).toFixed(2)}%`;

export const pnlColor = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? undefined : v > 0 ? "#33c088" : "#ef5b6b");

const NY = "America/New_York";

export const nyTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-US", { timeZone: NY, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) + " ET" : "—";

export const localTime = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";

/** "in 3h 12m" / "5m ago" relative to now. */
export function relative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diff = new Date(iso).getTime() - Date.now();
  const mins = Math.round(Math.abs(diff) / 60_000);
  const text = mins < 60 ? `${mins}m` : mins < 60 * 48 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${Math.round(mins / 1440)}d`;
  return diff >= 0 ? `in ${text}` : `${text} ago`;
}

export const STATUS_COLOR: Record<string, string> = { active: "green", paused: "orange", archived: "default" };
export const ACTION_COLOR: Record<string, string> = { BUY: "green", SELL: "red", HOLD: "default" };
export const SENTIMENT_COLOR: Record<string, string> = { bullish: "green", bearish: "red", neutral: "blue" };
export const ORDER_STATUS_COLOR: Record<string, string> = {
  filled: "green",
  partially_filled: "gold",
  new: "processing",
  submitted: "processing",
  canceled: "default",
  rejected: "red",
  failed: "red",
};
