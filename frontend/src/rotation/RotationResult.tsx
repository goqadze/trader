import { Alert, Card, Col, Progress, Row, Statistic, Table, Tag, Typography } from "antd";
import EquityChart from "../components/EquityChart";
import { VerdictAlert } from "../components/VerdictView";
import type { RotationRun } from "./types";

const MUTED = "#8b98b5";
const signed = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);

function Stat({ title, value, vs }: { title: string; value: string; vs?: string }) {
  return (
    <Statistic
      title={<span style={{ fontSize: 12 }}>{title}</span>}
      valueRender={() => (
        <span style={{ fontSize: 18 }}>
          {value}
          {vs && <span style={{ fontSize: 12, color: MUTED }}> vs {vs}</span>}
        </span>
      )}
    />
  );
}

/** One window of a rotation: the recommendation, the numbers against holding the whole universe, the equity
 *  curves, and what it held each month. */
export default function RotationResult({ title, run }: { title: string; run?: RotationRun }) {
  if (!run) return null;
  const r = run.result;
  const cfg = run.config;
  return (
    <Card
      size="small"
      style={{ marginBottom: 16 }}
      title={
        <span>
          {title} <span style={{ color: MUTED, fontWeight: 400, fontSize: 12 }}>{cfg.start} → {cfg.end}</span>
        </span>
      }
    >
      {run.status === "error" && <Alert type="error" showIcon message={run.error ?? "the run failed"} />}
      {!r && run.status !== "error" && <Progress percent={run.status === "running" ? 60 : 10} status="active" showInfo={false} />}
      {r && (
        <>
          {r.verdict && <VerdictAlert verdict={r.verdict} />}
          {r.missing_symbols.length > 0 && (
            <Alert type="warning" showIcon style={{ marginBottom: 16 }} message={`No prices for ${r.missing_symbols.join(", ")}: left out.`} />
          )}
          <Row gutter={[16, 8]} style={{ marginBottom: 8 }}>
            <Col xs={12} md={6}>
              <Stat title="Return" value={signed(r.total_return_pct)} vs={`${signed(r.buy_hold_return_pct)} held`} />
            </Col>
            <Col xs={12} md={6}>
              <Stat title="Worst drop" value={signed(r.max_drawdown_pct)} vs={signed(r.buy_hold_max_drawdown_pct)} />
            </Col>
            <Col xs={12} md={6}>
              <Stat title="Sharpe" value={r.sharpe?.toFixed(2) ?? "—"} vs={r.buy_hold_sharpe?.toFixed(2) ?? "—"} />
            </Col>
            <Col xs={12} md={6}>
              <Stat
                title="Round trips · won"
                value={`${r.num_trades}`}
                vs={`${r.win_rate_pct.toFixed(0)}% won · PF ${r.profit_factor == null ? "∞" : r.profit_factor.toFixed(2)}`}
              />
            </Col>
          </Row>
          <Typography.Text style={{ fontSize: 12, color: MUTED }}>
            "Held" = the {r.benchmark_symbols.length} symbols bought in equal parts on the first day and kept. Holding now:{" "}
            {r.held_at_end.join(", ") || "nothing"}.
          </Typography.Text>
          <EquityChart data={r.equity_curve.map((p) => ({ date: p.date, strategy: Math.round(p.equity), buyhold: Math.round(p.price) }))} />
          <Table
            size="small"
            rowKey="date"
            dataSource={[...r.rebalances].reverse()}
            pagination={{ pageSize: 6, size: "small" }}
            columns={[
              { title: "Rebalanced", dataIndex: "date", width: 110 },
              {
                title: "Held",
                dataIndex: "held",
                render: (held: string[]) => (held.length ? held.map((s) => <Tag key={s}>{s}</Tag>) : <span style={{ color: MUTED }}>cash</span>),
              },
              {
                title: "Strongest that day (momentum)",
                dataIndex: "ranking",
                render: (rk: { symbol: string; momentum_pct: number }[]) => (
                  <span style={{ fontSize: 12, color: MUTED }}>{rk.map((x) => `${x.symbol} ${signed(x.momentum_pct)}`).join(" · ")}</span>
                ),
              },
            ]}
          />
        </>
      )}
    </Card>
  );
}
