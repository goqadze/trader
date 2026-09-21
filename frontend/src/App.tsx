import { ConfigProvider, theme, Layout, Row, Col, Tag, Alert, Space } from "antd";
import ConfigForm from "./components/ConfigForm";
import StatTiles from "./components/StatTiles";
import EquityChart from "./components/EquityChart";
import ActivityLog from "./components/ActivityLog";
import { useBacktest } from "./hooks/useBacktest";
import type { RunStatus } from "./types";

const STATUS_COLOR: Record<RunStatus, string> = {
  idle: "default",
  starting: "processing",
  running: "processing",
  done: "success",
  error: "error",
};

// External dashboards for observability + API docs (localhost ports from docker-compose).
const LINKS: { label: string; href: string }[] = [
  { label: "Langfuse (LLM traces)", href: "http://localhost:3000" },
  { label: "GlitchTip (errors)", href: "http://localhost:8082" },
  { label: "Backtest API", href: "http://localhost:8001/docs" },
  { label: "Decision API", href: "http://localhost:8000/docs" },
];

export default function App() {
  const { state, start } = useBacktest();
  const running = state.status === "starting" || state.status === "running";

  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm, token: { colorPrimary: "#4f8cff" } }}>
      <Layout style={{ minHeight: "100vh" }}>
        <Layout.Header style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600, fontSize: 18 }}>📈 Backtest Monitor</span>
          <Tag color={STATUS_COLOR[state.status]}>{state.status}</Tag>
          {state.result && (
            <span style={{ color: "#8b98b5" }}>
              Return {state.result.total_return_pct}% vs B&amp;H {state.result.buy_hold_return_pct}% · maxDD {state.result.max_drawdown_pct}% · win {state.result.win_rate_pct}%
            </span>
          )}
          <Space size="large" style={{ marginLeft: "auto", fontSize: 13 }}>
            {LINKS.map((l) => (
              <a key={l.href} href={l.href} target="_blank" rel="noreferrer" style={{ color: "#8b98b5" }}>
                {l.label} ↗
              </a>
            ))}
          </Space>
        </Layout.Header>
        <Layout.Content style={{ padding: 16 }}>
          {state.error && <Alert type="error" message={state.error} showIcon style={{ marginBottom: 16 }} />}
          <Row gutter={[16, 16]}>
            <Col xs={24} md={7} lg={6}>
              <ConfigForm onRun={start} running={running} />
            </Col>
            <Col xs={24} md={17} lg={18}>
              <StatTiles state={state} />
              <EquityChart data={state.chart} />
              <ActivityLog rows={state.log} />
            </Col>
          </Row>
        </Layout.Content>
      </Layout>
    </ConfigProvider>
  );
}
