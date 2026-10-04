import { ExportOutlined } from "@ant-design/icons";
import { Button, Typography } from "antd";
import { strategyInfo, type IntradaySettings, type StrategyInfo } from "../strategies";

const MUTED = "#8b98b5";

/** The selected strategy's rules in words, a one-click "use its suggested settings", and a link to its guide
 *  section (a new tab, so a half-filled form isn't lost). */
export default function StrategyHelp({ id, onApply, onApplyIntraday }: {
  id: string | undefined;
  onApply: (s: StrategyInfo["suggested"]) => void;
  onApplyIntraday?: (s: IntradaySettings) => void;
}) {
  const s = id ? strategyInfo(id) : undefined;
  if (!s) return null;
  const g = s.suggested;
  const pct = (v: number) => `${+(v * 100).toFixed(2)}%`;
  return (
    <div style={{ fontSize: 12, color: MUTED, margin: "-12px 0 16px", lineHeight: 1.5 }}>
      <div>{s.summary}</div>
      <div><b>Buy:</b> {s.buy}</div>
      <div><b>Sell:</b> {s.sell}</div>
      <div><b>Best for:</b> {s.bestFor}</div>
      {s.intraday ? (
        <>
          <Typography.Text style={{ fontSize: 12, color: MUTED }}>
            Intraday on 5-minute bars, one setup a day, out by 15:55 · suggested: risk {pct(s.intraday.risk_pct)} a trade ·{" "}
            {s.intraday.sides === "both" ? "long and short" : "long only"} · slippage {pct(s.intraday.slippage_pct)} · backtest only (no live bots yet)
          </Typography.Text>
          {onApplyIntraday && (
            <Button type="link" size="small" style={{ fontSize: 12, paddingInline: 6 }} onClick={() => onApplyIntraday(s.intraday!)}>
              Apply
            </Button>
          )}
        </>
      ) : (
        <>
          <Typography.Text style={{ fontSize: 12, color: MUTED }}>
            Suggested: every {g.rebalance_days} day{g.rebalance_days === 1 ? "" : "s"} · stop {pct(g.stop_pct)} · target {pct(g.target_pct)}
            {g.decide_at ? ` · check ${g.decide_at === "both" ? "open + close" : `at the ${g.decide_at}`}` : ""}
          </Typography.Text>
          <Button type="link" size="small" style={{ fontSize: 12, paddingInline: 6 }} onClick={() => onApply(g)}>
            Apply
          </Button>
          <a href={`/guides/trading-strategies.html#${s.id}`} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            Chart &amp; details <ExportOutlined style={{ fontSize: 10 }} />
          </a>
        </>
      )}
    </div>
  );
}
