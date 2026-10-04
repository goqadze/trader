// The recommendation backtest-service attaches to every result (backtest-service/app/verdict.py decides it, so a
// scan can pick which combinations sit the exam): a short checklist of what a result needs before it is worth
// paper trading, and a grade from it. A backtest can at best earn "paper-trade it next".

/** "paper" = passed every check, "weak" = made money but missed a check, "no" = failed a must-have. */
export type Grade = "paper" | "weak" | "no";

export interface Check {
  ok: boolean;
  label: string;
  detail: string;
  must_have: boolean; // failing it means "don't trade this", whatever else passed
}

export interface Verdict {
  grade: Grade;
  title: string;
  summary: string;
  checks: Check[];
}

export const GRADE_LOOK: Record<Grade, { label: string; color: string; alert: "success" | "warning" | "error"; rank: number }> = {
  paper: { label: "Worth paper trading", color: "green", alert: "success", rank: 3 },
  weak: { label: "Not yet", color: "gold", alert: "warning", rank: 2 },
  no: { label: "Not recommended", color: "red", alert: "error", rank: 1 },
};
