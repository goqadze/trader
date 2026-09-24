import { AppstoreOutlined, DownOutlined, ExportOutlined, FundOutlined, ReadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, ConfigProvider, Dropdown, Layout, Menu, theme, type MenuProps } from "antd";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import BacktestPage from "./pages/BacktestPage";
import GuidePage from "./pages/GuidePage";
import BotPage from "./trading/BotPage";
import TradingPage from "./trading/TradingPage";

// External dashboards for observability + API docs (localhost ports from docker-compose).
const MONITORING = [
  { label: "Langfuse (LLM traces)", href: "http://localhost:3000" },
  { label: "GlitchTip (errors)", href: "http://localhost:8082" },
];
const API_DOCS = [
  { label: "Backtest API", href: "http://localhost:8001/docs" },
  { label: "Decision API", href: "http://localhost:8000/docs" },
  { label: "Trading API", href: "http://localhost:8002/docs" },
];

const external = (l: { label: string; href: string }) => ({
  key: l.href,
  label: (
    <a href={l.href} target="_blank" rel="noreferrer">
      {l.label} <ExportOutlined style={{ fontSize: 11, opacity: 0.6 }} />
    </a>
  ),
});

/** The "Resources" dropdown: the in-app guides first, then external tools (open in a new tab). */
const RESOURCES: MenuProps["items"] = [
  {
    type: "group",
    label: "Guides",
    children: [
      { key: "/guide", icon: <ReadOutlined />, label: <Link to="/guide">Trading Bot Lifecycle</Link> },
      { key: "/guide/strategies", icon: <FundOutlined />, label: <Link to="/guide/strategies">Trading Strategies</Link> },
    ],
  },
  { type: "group", label: "Monitoring", children: MONITORING.map(external) },
  { type: "group", label: "API docs", children: API_DOCS.map(external) },
];

/** Top bar: app name, page navigation, and the Resources dropdown. */
function Header() {
  const { pathname } = useLocation();
  const current = pathname.startsWith("/trading") ? "trading" : pathname === "/" ? "backtest" : "";
  return (
    <Layout.Header style={{ display: "flex", alignItems: "center", gap: 16 }}>
      <span style={{ fontWeight: 600, fontSize: 18, whiteSpace: "nowrap" }}>📈 Trading</span>
      <Menu
        theme="dark"
        mode="horizontal"
        selectedKeys={current ? [current] : []}
        style={{ flex: "1 1 auto", minWidth: 0, background: "transparent", borderBottom: "none" }}
        items={[
          { key: "backtest", label: <Link to="/">Backtest</Link> },
          { key: "trading", label: <Link to="/trading">Live trading</Link> },
        ]}
      />
      <Dropdown menu={{ items: RESOURCES, selectedKeys: [pathname] }} trigger={["click"]} placement="bottomRight">
        <Button type="text" icon={<AppstoreOutlined />} style={{ color: "#8b98b5" }}>
          Resources <DownOutlined style={{ fontSize: 10 }} />
        </Button>
      </Dropdown>
    </Layout.Header>
  );
}

export default function App() {
  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm, token: { colorPrimary: "#4f8cff" } }}>
      {/* AntApp provides message/modal/notification with the dark theme applied */}
      <AntApp>
        <BrowserRouter>
          <Layout style={{ minHeight: "100vh" }}>
            <Header />
            <Layout.Content style={{ padding: 16 }}>
              <Routes>
                <Route path="/" element={<BacktestPage />} />
                <Route path="/trading" element={<TradingPage />} />
                <Route path="/trading/:id" element={<BotPage />} />
                <Route path="/guide" element={<GuidePage guide="lifecycle" />} />
                <Route path="/guide/strategies" element={<GuidePage guide="strategies" />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Layout.Content>
          </Layout>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}
