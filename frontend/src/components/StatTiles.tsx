import { Row, Col, Card, Statistic, Progress } from "antd";
import type { BacktestState } from "../types";

// Top row of live KPIs plus the progress bar.
export default function StatTiles({ state }: { state: BacktestState }) {
  const ret = state.returnPct;
  return (
    <>
      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="Equity"
              value={state.equity ?? "—"}
              precision={state.equity != null ? 0 : undefined}
              prefix={state.equity != null ? "$" : ""}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Return" value={ret ?? 0} precision={2} suffix="%" valueStyle={{ color: (ret ?? 0) >= 0 ? "#33c088" : "#ef5b6b" }} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Position" value={state.position > 0 ? state.position : "flat"} suffix={state.position > 0 ? "sh" : ""} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Trades" value={state.trades} />
          </Card>
        </Col>
      </Row>
      {state.total > 0 && (
        <Progress percent={Math.round((state.i / state.total) * 100)} status={state.status === "running" ? "active" : undefined} style={{ marginTop: 16 }} />
      )}
    </>
  );
}
