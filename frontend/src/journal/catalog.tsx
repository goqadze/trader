// The journal's vocabulary: the emotions traders usually name, the classic mistakes, the stages of a trade, common
// setups. The journal stores the keys ("fomo", "moved-stop"); anything else the user types is kept as they wrote it.
// Also the small pieces every journal view shows them with.

import { Tag, Tooltip } from "antd";
import dayjs from "dayjs";
import type { JournalStage, JournalStatus } from "./types";

export const MUTED = "#8b98b5";
export const GREEN = "#33c088";
export const RED = "#ef5b6b";

/** helps: feelings that tend to help a trader; caution: fine, but worth noticing (they come before mistakes); hurts:
 *  the ones behind most bad trades. */
export type Tone = "helps" | "caution" | "hurts";

export interface Emotion {
  key: string;
  label: string;
  emoji: string;
  tone: Tone;
}

export const EMOTIONS: Emotion[] = [
  { key: "calm", label: "Calm", emoji: "😌", tone: "helps" },
  { key: "confident", label: "Confident", emoji: "💪", tone: "helps" },
  { key: "focused", label: "Focused", emoji: "🎯", tone: "helps" },
  { key: "patient", label: "Patient", emoji: "🧘", tone: "helps" },
  { key: "disciplined", label: "Disciplined", emoji: "📏", tone: "helps" },
  { key: "satisfied", label: "Satisfied", emoji: "😊", tone: "helps" },
  { key: "relieved", label: "Relieved", emoji: "😮‍💨", tone: "helps" },
  { key: "excited", label: "Excited", emoji: "🤩", tone: "caution" },
  { key: "euphoric", label: "Euphoric", emoji: "🥳", tone: "caution" },
  { key: "overconfident", label: "Overconfident", emoji: "😎", tone: "caution" },
  { key: "hopeful", label: "Hoping", emoji: "🙏", tone: "caution" },
  { key: "doubtful", label: "Doubtful", emoji: "🤔", tone: "caution" },
  { key: "bored", label: "Bored", emoji: "🥱", tone: "caution" },
  { key: "tired", label: "Tired", emoji: "😴", tone: "caution" },
  { key: "fomo", label: "FOMO", emoji: "😱", tone: "hurts" },
  { key: "greedy", label: "Greedy", emoji: "🤑", tone: "hurts" },
  { key: "anxious", label: "Anxious", emoji: "😰", tone: "hurts" },
  { key: "fearful", label: "Afraid", emoji: "😨", tone: "hurts" },
  { key: "impatient", label: "Impatient", emoji: "⏳", tone: "hurts" },
  { key: "frustrated", label: "Frustrated", emoji: "😤", tone: "hurts" },
  { key: "revenge", label: "Want revenge", emoji: "😡", tone: "hurts" },
  { key: "regretful", label: "Regret", emoji: "😞", tone: "hurts" },
  { key: "disappointed", label: "Disappointed", emoji: "😔", tone: "hurts" },
];
const EMOTION_BY_KEY = new Map(EMOTIONS.map((e) => [e.key, e]));
export const TONE_COLOR: Record<Tone, string> = { helps: "green", caution: "gold", hurts: "red" };
export const TONE_LABEL: Record<Tone, string> = { helps: "Helpful", caution: "Worth noticing", hurts: "Risky" };

/** An emotion's catalog entry, or one made up for a word the user typed (no emoji, a neutral tone). */
export const emotion = (key: string): Emotion | { key: string; label: string; emoji: string; tone: null } =>
  EMOTION_BY_KEY.get(key) ?? { key, label: key.charAt(0).toUpperCase() + key.slice(1), emoji: "", tone: null };

export const emotionLabel = (key: string) => {
  const e = emotion(key);
  return e.emoji ? `${e.emoji} ${e.label}` : e.label;
};

export function EmotionTag({ name }: { name: string }) {
  const e = emotion(name);
  return (
    <Tag color={e.tone ? TONE_COLOR[e.tone] : undefined} style={{ marginInlineEnd: 4 }}>
      {e.emoji && <span style={{ marginRight: 4 }}>{e.emoji}</span>}
      {e.label}
    </Tag>
  );
}

/** Only the emojis (with the names on hover): compact, for table rows. */
export function EmotionEmojis({ names, max = 6 }: { names: string[]; max?: number }) {
  if (!names.length) return <span style={{ color: MUTED }}>—</span>;
  const shown = names.slice(0, max);
  return (
    <Tooltip title={names.map(emotionLabel).join(", ")}>
      <span style={{ whiteSpace: "nowrap", letterSpacing: 2 }}>
        {shown.map((n) => emotion(n).emoji || <Tag key={n} style={{ marginInlineEnd: 2, fontSize: 11 }}>{emotion(n).label}</Tag>)}
        {names.length > max && <span style={{ color: MUTED, fontSize: 12 }}> +{names.length - max}</span>}
      </span>
    </Tooltip>
  );
}

export const MISTAKES: { key: string; label: string }[] = [
  { key: "no-plan", label: "No plan" },
  { key: "fomo-entry", label: "Chased it (FOMO entry)" },
  { key: "early-entry", label: "Entered too early" },
  { key: "late-entry", label: "Entered too late" },
  { key: "no-stop", label: "No stop-loss" },
  { key: "moved-stop", label: "Moved my stop" },
  { key: "oversized", label: "Position too big" },
  { key: "early-exit", label: "Sold too early" },
  { key: "late-exit", label: "Held too long / gave back profit" },
  { key: "averaged-down", label: "Added to a loser" },
  { key: "revenge", label: "Revenge trade" },
  { key: "overtrading", label: "Overtrading" },
  { key: "ignored-rules", label: "Broke my rules" },
  { key: "news", label: "Traded into news / earnings" },
  { key: "distracted", label: "Distracted, not focused" },
];
const MISTAKE_BY_KEY = new Map(MISTAKES.map((m) => [m.key, m.label]));
export const mistakeLabel = (key: string) => MISTAKE_BY_KEY.get(key) ?? key;

// color: the tag's preset color; dot: the timeline's (a CSS color)
export const STAGES: Record<JournalStage, { label: string; emoji: string; color: string; dot: string; hint: string }> = {
  plan: { label: "Plan", emoji: "📝", color: "blue", dot: "#4f8cff", hint: "Before entering: why this trade, and how you feel about it" },
  entry: { label: "Entry", emoji: "🚪", color: "cyan", dot: "#13c2c2", hint: "As you get in" },
  during: { label: "During", emoji: "👀", color: "purple", dot: "#9254de", hint: "While it runs: the urge to move the stop, to sell early ..." },
  exit: { label: "Exit", emoji: "🏁", color: "magenta", dot: "#eb2f96", hint: "As you get out" },
  review: { label: "Review", emoji: "🔍", color: "geekblue", dot: "#597ef7", hint: "Looking back, calmly" },
  checkin: { label: "Check-in", emoji: "💬", color: "default", dot: MUTED, hint: "How you feel right now, about no trade in particular" },
};

export function StageTag({ stage }: { stage: JournalStage }) {
  const s = STAGES[stage];
  return <Tag color={s.color} style={{ marginInlineEnd: 0 }}>{s.emoji} {s.label}</Tag>;
}

export const STATUS: Record<JournalStatus, { label: string; color: string }> = {
  planned: { label: "Planned", color: "blue" },
  open: { label: "Open", color: "processing" },
  closed: { label: "Closed", color: "default" },
  skipped: { label: "Skipped", color: "warning" },
};

export function StatusTag({ status }: { status: JournalStatus }) {
  return <Tag color={STATUS[status].color} style={{ marginInlineEnd: 0 }}>{STATUS[status].label}</Tag>;
}

/** Common setups, offered next to the ones the user wrote before. */
export const SETUPS = [
  "Dip buy", "Breakout", "Pullback", "Trend following", "Momentum", "Mean reversion", "Range", "Reversal", "Gap and go",
  "News catalyst", "Earnings", "Momentum rotation", "Long-term hold",
];

/** 1-5 ratings in words (mood, energy, sleep, grades, confidence). */
export const RATING_WORDS: Record<string, string[]> = {
  mood: ["Awful", "Low", "Okay", "Good", "Great"],
  energy: ["Exhausted", "Tired", "Okay", "Fresh", "Full of energy"],
  sleep: ["Barely slept", "Poorly", "Okay", "Well", "Very well"],
  grade: ["F · Poor", "D · Weak", "C · Okay", "B · Good", "A · Excellent"],
  confidence: ["Very unsure", "Unsure", "Neutral", "Confident", "Very confident"],
  intensity: ["Barely", "A little", "Clearly", "Strongly", "Overwhelming"],
};
export const MOOD_EMOJI = ["😣", "🙁", "😐", "🙂", "😄"];
export const GRADE_LETTER = ["F", "D", "C", "B", "A"];

/** The New York date today: the journal's trading days are New York dates, like the market's. */
export const todayNY = () => new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });

/** "Mon, Jun 2" (or "Mon, Jun 2, 2025" outside this year). */
export const dayLabel = (day: string) => dayjs(day).format(dayjs(day).year() === dayjs().year() ? "ddd, MMM D" : "ddd, MMM D, YYYY");

/** "3h 20m", "2d 4h", "45m". */
export function holdText(minutes: number | null | undefined): string {
  if (minutes == null) return "—";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  return `${Math.floor(minutes / 1440)}d ${Math.floor((minutes % 1440) / 60)}h`;
}

/** +1.25R / -1.00R */
export const rText = (r: number | null | undefined) => (r == null ? "—" : `${r >= 0 ? "+" : ""}${r.toFixed(2)}R`);

/** A price: up to 4 decimals for cheap ones, 2 otherwise. */
export const price = (v: number | null | undefined) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: Math.abs(v) < 10 ? 4 : 2 })}`;

export const qtyText = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: 6 }));

/** For the calendar's small cells (a phone fits ~6 characters): +$44, -$101, +$1.2k, +$12k, +$0.40. */
export function compactUsd(v: number): string {
  const a = Math.abs(v);
  const s = a >= 9_950 ? `${(a / 1000).toFixed(0)}k` : a >= 999.5 ? `${(a / 1000).toFixed(1)}k` : a >= 9.5 ? a.toFixed(0) : a.toFixed(a >= 1 ? 1 : 2);
  return `${v < 0 ? "-" : v > 0 ? "+" : ""}$${s}`;
}
