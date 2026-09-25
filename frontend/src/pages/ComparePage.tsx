import { LoadingOutlined } from "@ant-design/icons";
import { Alert, Card, Col, Empty, Row, Tabs, Typography } from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import CompareChart from "../compare/CompareChart";
import CompareForm from "../compare/CompareForm";
import CompareTable from "../compare/CompareTable";
import StrategyDetail from "../compare/StrategyDetail";
import { useCompare } from "../hooks/useCompare";
import { strategyShortName, type StrategyId } from "../strategies";
import type { BacktestState, Result } from "../types";

const MUTED = "#8b98b5";
const signed = (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

/** The winners in words: best return, best risk-adjusted, smallest drawdown, and who beat buy & hold. */
function Verdict({ done, total, days }: { done: [StrategyId, Result][]; total: number; days: number }) {
  if (done.length < 2) return null;
  const top = (f: (r: Result) => number | null | undefined) =>
    done.filter(([, r]) => f(r) != null).sort(([, a], [, b]) => f(b)! - f(a)!)[0];
  const byReturn = top((r) => r.total_return_pct);
  const bySharpe = top((r) => r.sharpe);
  const byDrawdown = top((r) => r.max_drawdown_pct);
  const bh = done[0][1].buy_hold_return_pct;
  const beat = done.filter(([, r]) => r.beat_buy_hold).length;
  const finished = done.length === total;
  return (
    <Alert
      type={beat > 0 ? "success" : "warning"}
      showIcon
      style={{ marginBottom: 16 }}
      message={
        <span>
          {finished ? "" : `So far (${done.length} of ${total} finished): `}
          Best return <b>{strategyShortName(byReturn[0])}</b> {signed(byReturn[1].total_return_pct)}
          {bySharpe && (
            <>
              {" · "}best risk-adjusted <b>{strategyShortName(bySharpe[0])}</b> (Sharpe {bySharpe[1].sharpe})
            </>
          )}
          {" · "}smallest drawdown <b>{strategyShortName(byDrawdown[0])}</b> {byDrawdown[1].max_drawdown_pct}%
          {" · "}
          {beat} of {done.length} beat buy &amp; hold ({signed(bh)})
        </span>
      }
      description={
        days < 180
          ? `${days} days is a short window: a handful of trades decide these numbers, and a strategy that waits for its setup may not have had one. Check the leaders over a longer range (a year or two) before trusting them.`
          : "Past results on one symbol don't guarantee the future. Paper-trade the winner before risking real money."
      }
    />
  );
}

function tabLabel(id: StrategyId, run: BacktestState | undefined) {
  const r = run?.result;
  return (
    <span>
      {strategyShortName(id)}{" "}
      {r ? (
        <span style={{ color: r.total_return_pct >= 0 ? "#33c088" : "#ef5b6b", fontSize: 12 }}>{signed(r.total_return_pct)}</span>
      ) : run?.status === "error" ? (
        <span style={{ color: "#ef5b6b", fontSize: 12 }}>error</span>
      ) : (
        <LoadingOutlined style={{ fontSize: 11, color: MUTED }} />
      )}
    </span>
  );
}

/** Run several strategies on one symbol and window, then compare them: a summary table, one chart with
 *  every strategy, and a tab per strategy with its own tiles, equity chart, trades and activity log. */
export default function ComparePage() {
  const { state, start } = useCompare();
  const [focus, setFocus] = useState<StrategyId>();
  const { order, runs } = state;
  const active = focus && order.includes(focus) ? focus : order[0];
  const running = order.some((id) => runs[id]?.status === "starting" || runs[id]?.status === "running");
  const done = order.flatMap((id): [StrategyId, Result][] => (runs[id]?.result ? [[id, runs[id]!.result!]] : []));
  const cfg = order.length ? runs[order[0]]?.config : undefined;
  const days = cfg ? dayjs(cfg.end).diff(dayjs(cfg.start), "day") : 0;

  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={7} lg={6}>
        <CompareForm onRun={start} running={running} />
      </Col>
      <Col xs={24} md={17} lg={18}>
        {order.length === 0 ? (
          <Card>
            <Empty
              description={
                <span style={{ color: MUTED }}>
                  Pick the strategies to compare and run them on one symbol. Each gets its own backtest; the results land
                  side by side in one table.
                </span>
              }
            />
          </Card>
        ) : (
          <>
            <Verdict done={done} total={order.length} days={days} />
            <Card
              title={`${cfg?.symbol} · ${cfg?.start} → ${cfg?.end}`}
              size="small"
              extra={<Typography.Text style={{ color: MUTED, fontSize: 12 }}>Best in each column in green · click a row for its details</Typography.Text>}
            >
              <CompareTable order={order} runs={runs} focus={active} onFocus={setFocus} />
            </Card>
            <CompareChart order={order} runs={runs} focus={active} />
            <Card size="small" style={{ marginTop: 16 }}>
              <Tabs
                activeKey={active}
                onChange={(k) => setFocus(k as StrategyId)}
                destroyInactiveTabPane
                items={order.map((id) => ({ key: id, label: tabLabel(id, runs[id]), children: runs[id] && <StrategyDetail run={runs[id]!} /> }))}
              />
            </Card>
          </>
        )}
      </Col>
    </Row>
  );
}
