import { App as AntApp, ConfigProvider, Layout, Menu, Space, theme } from "antd";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import BacktestPage from "./pages/BacktestPage";
import BotPage from "./trading/BotPage";
import TradingPage from "./trading/TradingPage";

// External dashboards for observability + API docs (localhost ports from docker-compose).
const LINKS: { label: string; href: string }[] = [
  { label: "Langfuse (LLM traces)", href: "http://localhost:3000" },
  { label: "GlitchTip (errors)", href: "http://localhost:8082" },
  { label: "Backtest API", href: "http://localhost:8001/docs" },
  { label: "Decision API", href: "http://localhost:8000/docs" },
  { label: "Trading API", href: "http://localhost:8002/docs" },
];

/** Top bar: app name, page navigation, external tool links. */
function Header() {
  const { pathname } = useLocation();
  const current = pathname.startsWith("/trading") ? "trading" : "backtest";
  return (
    <Layout.Header style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap", height: "auto", minHeight: 64 }}>
      <span style={{ fontWeight: 600, fontSize: 18, whiteSpace: "nowrap" }}>📈 Trading</span>
      <Menu
        theme="dark"
        mode="horizontal"
        selectedKeys={[current]}
        style={{ flex: "0 0 auto", minWidth: 220, background: "transparent", borderBottom: "none" }}
        items={[
          { key: "backtest", label: <Link to="/">Backtest</Link> },
          { key: "trading", label: <Link to="/trading">Live trading</Link> },
        ]}
      />
      <Space size="large" wrap style={{ marginLeft: "auto", fontSize: 13 }}>
        {LINKS.map((l) => (
          <a key={l.href} href={l.href} target="_blank" rel="noreferrer" style={{ color: "#8b98b5" }}>
            {l.label} ↗
          </a>
        ))}
      </Space>
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
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Layout.Content>
          </Layout>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}
