import { CheckCircleFilled, CloseCircleFilled } from "@ant-design/icons";
import { Alert, Tag, Tooltip } from "antd";
import type { ReactNode } from "react";
import { GRADE_LOOK, type Verdict } from "../verdict";

const MUTED = "#8b98b5";

/** The grade as a small coloured tag; hover shows the summary. */
export function VerdictTag({ verdict }: { verdict: Verdict }) {
  return (
    <Tooltip title={verdict.summary}>
      <Tag color={GRADE_LOOK[verdict.grade].color} style={{ margin: 0 }}>
        {verdict.title}
      </Tag>
    </Tooltip>
  );
}

/** The summary plus every check with a tick or a cross. */
export function VerdictChecks({ verdict }: { verdict: Verdict }) {
  return (
    <div style={{ lineHeight: 1.6 }}>
      <div>{verdict.summary}</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(320px, 100%), 1fr))", gap: "2px 16px", marginTop: 6 }}>
        {verdict.checks.map((c) => (
          <div key={c.label} style={{ fontSize: 12 }}>
            {c.ok ? <CheckCircleFilled style={{ color: "#33c088" }} /> : <CloseCircleFilled style={{ color: "#ef5b6b" }} />}{" "}
            <b>{c.label}</b>
            {c.must_have && !c.ok && " (must-have)"}: <span style={{ color: MUTED }}>{c.detail}</span>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 11, color: MUTED, marginTop: 6 }}>
        Rules of thumb from one backtest, not financial advice.
      </div>
    </div>
  );
}

/** The recommendation as an alert box: "Recommendation: <grade>" with the checklist. */
export function VerdictAlert({ verdict, extra, action }: { verdict: Verdict; extra?: ReactNode; action?: ReactNode }) {
  return (
    <Alert
      type={GRADE_LOOK[verdict.grade].alert}
      showIcon
      style={{ marginBottom: 16 }}
      message={
        <span>
          Recommendation: <b>{verdict.title}</b>
          {extra}
        </span>
      }
      description={<VerdictChecks verdict={verdict} />}
      action={action}
    />
  );
}
