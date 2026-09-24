import { Button, Typography } from "antd";
import { strategyInfo, type StrategyInfo } from "../strategies";

const MUTED = "#8b98b5";

/** The selected strategy's rules in words, plus a one-click "use its suggested settings". */
export default function StrategyHelp({ id, onApply }: { id: string | undefined; onApply: (s: StrategyInfo["suggested"]) => void }) {
  const s = id ? strategyInfo(id) : undefined;
  if (!s) return null;
  const g = s.suggested;
  const pct = (v: number) => `${+(v * 100).toFixed(1)}%`;
  return (
    <div style={{ fontSize: 12, color: MUTED, margin: "-12px 0 16px", lineHeight: 1.5 }}>
      <div>{s.summary}</div>
      <div><b>Buy:</b> {s.buy}</div>
      <div><b>Sell:</b> {s.sell}</div>
      <div><b>Best for:</b> {s.bestFor}</div>
      <Typography.Text style={{ fontSize: 12, color: MUTED }}>
        Suggested: every {g.rebalance_days} day{g.rebalance_days === 1 ? "" : "s"} · stop {pct(g.stop_pct)} · target {pct(g.target_pct)}
        {g.decide_at ? ` · check ${g.decide_at === "both" ? "open + close" : `at the ${g.decide_at}`}` : ""}
      </Typography.Text>
      <Button type="link" size="small" style={{ fontSize: 12, paddingInline: 6 }} onClick={() => onApply(g)}>
        Apply
      </Button>
    </div>
  );
}
