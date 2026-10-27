// What a strategy brings to the shared Test results grid and saved-setups picker (dip/kit.tsx, rotation/kit.tsx).

import type { ReactNode } from "react";
import type { SavedSetup, StudyStrategy } from "./types";

/** The test windows' names (the grid's columns, a bot's tested setup) */
export const PERIOD_LABEL: Record<string, string> = {
  practice: "Practice", exam: "Exam", stress: "Stress check", early: "Early check", practice4: "Practice", exam4: "Exam",
  bear: "Bear 2022",
};

/** A rule the tests of a batch can differ in, as a grid column (trading-service studies.RULES: the same keys). */
export interface RuleColumn<C> {
  key: string;
  title: string;
  tip: string;
  value: (c: C) => number;
  text: (c: C) => string;
}

export interface StrategyKit<C extends { symbols: string[] }> {
  strategy: StudyStrategy;
  rules: RuleColumn<C>[];
  /** The setup in words, above an opened test's charts. */
  describe: (c: C) => ReactNode;
  /** One line under a saved setup's name in the picker. */
  summary: (c: C) => string;
  /** Two setups are the same when these match ("the last n months" by n, custom periods by their dates). */
  canon: (c: Partial<C>) => string;
  /** A setup with any rule added since it was saved filled in with its default, ready for a form. */
  complete: (c: C) => C;
  presets: {
    list: () => Promise<SavedSetup<C>[]>;
    save: (name: string, config: C, testId?: number) => Promise<SavedSetup<C>>; // testId: saved from that test
    remove: (id: number) => Promise<void>;
  };
  /** What a saved setup keeps, in words: "the rules, the watchlist, ..." */
  keeps: string;
  /** The separate short tests' column: "Quarters" (Q1 ...) or "Years" (Y1 ...). */
  quarters: { title: string; tip: string; short: string };
  /** An opened test's per-symbol table counts stop-losses. */
  stops: boolean;
  /** "watchlist" or "universe" */
  listWord: string;
  /** A window's tooltip where the strategy's batches mean something else by it (n = its months). */
  periodTips?: Record<string, (n: number) => string>;
}
