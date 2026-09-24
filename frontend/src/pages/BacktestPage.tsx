import { RocketOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Col, Row, Space, Tag } from "antd";
import { useNavigate } from "react-router-dom";
import ActivityLog from "../components/ActivityLog";
import ConfigForm from "../components/ConfigForm";
import EquityChart from "../components/EquityChart";
import StatTiles from "../components/StatTiles";
import { useBacktest } from "../hooks/useBacktest";
import type { BotFormInitial } from "../trading/BotForm";
import { strategyName } from "../strategies";
import type { RunConfig, RunStatus } from "../types";

const STATUS_COLOR: Record<RunStatus, string> = {
  idle: "default",
  starting: "processing",
  running: "processing",
  done: "success",
  error: "error",
};

/** Backtest config -> trading bot form values. Same parameter names, so nothing is lost in between. */
function toBotParams(cfg: RunConfig): BotFormInitial {
  return {
    symbol: cfg.symbol,
    name: `${cfg.symbol} ${strategyName(cfg.strategy)} (backtest ${cfg.start}..${cfg.end})`.slice(0, 80),
    allocated_cash: cfg.initial_cash,
    strategy: cfg.strategy,
    min_confidence: cfg.min_confidence,
    rebalance_days: cfg.rebalance_days,
    position_pct: cfg.position_pct,
    ...(cfg.stop_pct != null && { stop_pct: cfg.stop_pct }),
    ...(cfg.target_pct != null && { target_pct: cfg.target_pct }),
  };
}

/** Configure a backtest on the left, watch it run live on the right. */
export default function BacktestPage() {
  const { state, start } = useBacktest();
  const navigate = useNavigate();
  const running = state.status === "starting" || state.status === "running";
  const r = state.result;

  return (
    <>
      {state.error && <Alert type="error" message={state.error} showIcon style={{ marginBottom: 16 }} />}
      {r && state.config && (
        <Alert
          type={r.alpha_vs_buy_hold_pct != null && r.alpha_vs_buy_hold_pct < 0 ? "warning" : "success"}
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space wrap>
              <Tag color={STATUS_COLOR[state.status]}>{state.status}</Tag>
              <span>
                Return {r.total_return_pct}% vs buy &amp; hold {r.buy_hold_return_pct}% · max drawdown {r.max_drawdown_pct}% ·
                win rate {r.win_rate_pct}% over {r.num_trades} trades
              </span>
            </Space>
          }
          description={
            r.alpha_vs_buy_hold_pct != null && r.alpha_vs_buy_hold_pct < 0
              ? "This setup lagged buy & hold. You can still paper-trade it, but test other parameters first."
              : "Happy with it? Paper-trade the exact same parameters on live prices before risking real money."
          }
          action={
            <Button type="primary" icon={<RocketOutlined />} onClick={() => navigate("/trading", { state: { prefill: toBotParams(state.config!) } })}>
              Trade this strategy
            </Button>
          }
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
