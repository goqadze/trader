import { Alert, Card, Col, Row, Tag } from "antd";
import ActivityLog from "../components/ActivityLog";
import ConfigForm from "../components/ConfigForm";
import EquityChart from "../components/EquityChart";
import StatTiles from "../components/StatTiles";
import TradeThisButton from "../components/TradeThisButton";
import { VerdictAlert } from "../components/VerdictView";
import { useBacktest } from "../hooks/useBacktest";
import type { RunStatus } from "../types";
import { judge } from "../verdict";

const STATUS_COLOR: Record<RunStatus, string> = {
  idle: "default",
  starting: "processing",
  running: "processing",
  done: "success",
  error: "error",
};

/** Configure a backtest on the left, watch it run live on the right. */
export default function BacktestPage() {
  const { state, start } = useBacktest();
  const running = state.status === "starting" || state.status === "running";
  const r = state.result;

  return (
    <>
      {state.error && <Alert type="error" message={state.error} showIcon style={{ marginBottom: 16 }} />}
      {r && state.config && (
        <VerdictAlert
          verdict={judge(r, state.config)}
          extra={
            <span style={{ fontWeight: 400 }}>
              {" "}
              · return {r.total_return_pct}% vs buy &amp; hold {r.buy_hold_return_pct}% · max drawdown {r.max_drawdown_pct}% ·
              win rate {r.win_rate_pct}% over {r.num_trades} trades
            </span>
          }
          action={<TradeThisButton config={state.config} />}
        />
      )}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={7} lg={6}>
          <ConfigForm onRun={start} running={running} />
        </Col>
        <Col xs={24} md={17} lg={18}>
          {!r && state.status !== "idle" && (
            <Card size="small" style={{ marginBottom: 16 }}>
              <Tag color={STATUS_COLOR[state.status]}>{state.status}</Tag> {state.symbol}
            </Card>
          )}
          <StatTiles state={state} />
          <EquityChart data={state.chart} />
          <ActivityLog rows={state.log} />
        </Col>
      </Row>
    </>
  );
}
