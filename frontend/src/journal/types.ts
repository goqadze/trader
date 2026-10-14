// The trading journal's shapes, as trading-service journal.py sends them (schemas.Journal*).

export type JournalStatus = "planned" | "open" | "closed" | "skipped";
export type JournalSide = "long" | "short";
/** When a note was written: a step of a trade, or a check-in (the day's, no trade). */
export type JournalStage = "plan" | "entry" | "during" | "exit" | "review" | "checkin";

export interface JournalNote {
  id: number;
  trade_id: number | null;
  symbol: string | null; // the trade's (null: a check-in)
  at: string;
  stage: JournalStage;
  emotions: string[];
  intensity: number | null; // 1-5
  text: string;
  created_at: string;
}

export interface JournalNoteIn {
  stage: JournalStage;
  at?: string;
  emotions: string[];
  intensity?: number | null;
  text: string;
  trade_id?: number | null;
}

/** The fields of a trade's form (everything the user sets). */
export interface JournalTradeFields {
  status: JournalStatus;
  symbol: string;
  side: JournalSide;
  account: string;
  setup: string;
  tags: string[];
  quantity: number | null;
  entry_price: number | null;
  stop_price: number | null; // the first stop: one R of risk
  target_price: number | null;
  confidence: number | null; // 1-5
  thesis: string;
  opened_at: string | null;
  closed_at: string | null;
  exit_price: number | null;
  fees: number;
  exit_reason: string;
  followed_plan: boolean | null;
  grade: number | null; // 1-5
  mistakes: string[];
  review: string;
  lessons: string;
}

export interface JournalTrade extends JournalTradeFields {
  id: number;
  bot_id: number | null;
  pnl: number | null; // closed: after fees
  r_multiple: number | null;
  source: "manual" | "bot";
  created_at: string;
  updated_at: string;
  day: string; // its trading day (New York): of the exit, else the entry, else when it was written
  risk: number | null;
  reward_risk: number | null;
  return_pct: number | null;
  hold_minutes: number | null;
  emotions: string[]; // every emotion in its notes, first felt first
  notes: number;
  images: number;
}

export interface JournalImage {
  id: number;
  trade_id: number;
  created_at: string;
  content_type: string;
  size: number;
  caption: string;
}

export interface JournalTradeDetail extends JournalTrade {
  note_list: JournalNote[];
  image_list: JournalImage[];
  bot_name: string | null;
}

export type JournalTradeCreate = Partial<JournalTradeFields> & { symbol: string; note?: JournalNoteIn | null };
export type JournalTradeUpdate = Partial<JournalTradeFields> & { note?: JournalNoteIn | null };

export interface JournalDayFields {
  mood: number | null;
  energy: number | null;
  sleep: number | null;
  plan: string;
  review: string;
  lessons: string;
  grade: number | null;
  followed_rules: boolean | null;
}

export interface JournalDay extends JournalDayFields {
  day: string;
  updated_at: string | null; // null: nothing written yet
  notes: JournalNote[];
  trades: JournalTrade[];
  pnl: number;
}

export interface JournalCalendarDay {
  day: string;
  pnl: number;
  closed: number;
  wins: number;
  losses: number;
  opened: number;
  notes: number;
  mood: number | null;
  grade: number | null;
  page: boolean;
}

export interface JournalGroup {
  key: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  pnl: number;
  avg_pnl: number;
  avg_r: number | null;
}

export interface JournalStats {
  trades: number;
  open: number;
  planned: number;
  skipped: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate: number | null;
  net_pnl: number;
  gross_profit: number;
  gross_loss: number;
  fees: number;
  profit_factor: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  payoff_ratio: number | null;
  expectancy: number | null;
  avg_r: number | null;
  total_r: number | null;
  r_trades: number;
  best: { id: number; symbol: string; pnl: number; day: string } | null;
  worst: { id: number; symbol: string; pnl: number; day: string } | null;
  max_win_streak: number;
  max_loss_streak: number;
  current_streak: number;
  max_drawdown: number;
  avg_hold_minutes_win: number | null;
  avg_hold_minutes_loss: number | null;
  daily: { day: string; pnl: number; trades: number; cumulative: number }[];
  by_setup: JournalGroup[];
  by_tag: JournalGroup[];
  by_mistake: JournalGroup[];
  by_emotion: JournalGroup[];
  by_symbol: JournalGroup[];
  by_side: JournalGroup[];
  by_weekday: JournalGroup[];
  by_hour: JournalGroup[];
  by_grade: JournalGroup[];
  by_confidence: JournalGroup[];
  by_plan: JournalGroup[];
  by_account: JournalGroup[];
  emotions: { emotion: string; count: number }[];
}

export interface JournalOptions {
  symbols: string[];
  accounts: string[];
  setups: string[];
  tags: string[];
  mistakes: string[];
  emotions: string[];
}

export interface JournalImportBot {
  id: number;
  name: string;
  strategy: string;
  broker: string;
  live: boolean;
  status: string;
  closed_trades: number;
  imported: number;
}

/** The list's filters (the statistics and the CSV export use the same). Dates are trading days, both ends included. */
export interface JournalFilters {
  status?: JournalStatus;
  q?: string;
  setup?: string;
  tag?: string;
  mistake?: string;
  emotion?: string;
  account?: string;
  source?: "manual" | "bot";
  side?: JournalSide;
  result?: "win" | "loss" | "breakeven";
  start?: string;
  end?: string;
}

export interface JournalPage {
  total: number;
  items: JournalTrade[];
}
