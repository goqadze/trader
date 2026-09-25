import { AppstoreOutlined, DownOutlined, ExportOutlined, FundOutlined, ReadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, ConfigProvider, Dropdown, Layout, Menu, theme, type MenuProps } from "antd";
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AccountMenu, UsersNavLabel } from "./auth/AccountMenu";
import { AuthProvider, RequireAuth, useAuth } from "./auth/AuthContext";
import { LoginPage, SignupPage } from "./auth/AuthPages";
import UsersPage from "./auth/UsersPage";
import BacktestPage from "./pages/BacktestPage";
import ComparePage from "./pages/ComparePage";
import GuidePage from "./pages/GuidePage";
import BotPage from "./trading/BotPage";
import TradingPage from "./trading/TradingPage";

// External dashboards for observability, API docs and the database browser (localhost ports from docker-compose).
const MONITORING = [
  { label: "Langfuse (LLM traces)", href: "http://localhost:3000" },
  { label: "GlitchTip (errors)", href: "http://localhost:8082" },
];
const API_DOCS = [
  { label: "Backtest API", href: "http://localhost:8001/docs" },
  { label: "Decision API", href: "http://localhost:8000/docs" },
  { label: "Trading API", href: "http://localhost:8002/docs" },
];
const DATABASES = [{ label: "pgAdmin (tables & data)", href: "http://localhost:5050" }];

const external = (l: { label: string; href: string }) => ({
  key: l.href,
  label: (
    <a href={l.href} target="_blank" rel="noreferrer">
      {l.label} <ExportOutlined style={{ fontSize: 11, opacity: 0.6 }} />
    </a>
  ),
});

const GUIDES: NonNullable<MenuProps["items"]> = [
  {
    type: "group",
    label: "Guides",
    children: [
      { key: "/guide", icon: <ReadOutlined />, label: <Link to="/guide">Trading Bot Lifecycle</Link> },
      { key: "/guide/strategies", icon: <FundOutlined />, label: <Link to="/guide/strategies">Trading Strategies</Link> },
    ],
  },
];

/** The "Resources" dropdown: the in-app guides first, then external tools (open in a new tab). The tools
 * are localhost links on the machine running the stack, so only admins see them. */
const resources = (admin: boolean): MenuProps["items"] =>
  admin
    ? [
        ...GUIDES,
        { type: "group", label: "Monitoring", children: MONITORING.map(external) },
        { type: "group", label: "API docs", children: API_DOCS.map(external) },
        { type: "group", label: "Databases", children: DATABASES.map(external) },
      ]
    : GUIDES;

/** Top bar: app name, page navigation, the Resources dropdown, and the account menu. */
function Header() {
  const { pathname } = useLocation();
  const admin = useAuth().user?.role === "admin";
  const current = pathname.startsWith("/trading")
    ? "trading"
    : pathname.startsWith("/admin/users")
      ? "users"
      : pathname === "/compare"
        ? "compare"
        : pathname === "/"
          ? "backtest"
          : "";
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
          { key: "compare", label: <Link to="/compare">Compare strategies</Link> },
          { key: "trading", label: <Link to="/trading">Live trading</Link> },
          ...(admin ? [{ key: "users", label: <Link to="/admin/users"><UsersNavLabel /></Link> }] : []),
        ]}
      />
      <Dropdown menu={{ items: resources(admin), selectedKeys: [pathname] }} trigger={["click"]} placement="bottomRight">
        <Button type="text" icon={<AppstoreOutlined />} style={{ color: "#8b98b5" }}>
          Resources <DownOutlined style={{ fontSize: 10 }} />
        </Button>
      </Dropdown>
      <AccountMenu />
    </Layout.Header>
  );
}

/** Every signed-in page: the top bar above the current page. */
function Shell() {
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header />
      <Layout.Content style={{ padding: 16 }}>
        <Outlet />
      </Layout.Content>
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm, token: { colorPrimary: "#4f8cff" } }}>
      {/* AntApp provides message/modal/notification with the dark theme applied */}
      <AntApp>
        <BrowserRouter>
          <AuthProvider>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/signup" element={<SignupPage />} />
              {/* Everything else needs a signed-in user */}
              <Route element={<RequireAuth><Shell /></RequireAuth>}>
                <Route path="/" element={<BacktestPage />} />
                <Route path="/compare" element={<ComparePage />} />
                <Route path="/trading" element={<TradingPage />} />
                <Route path="/trading/:id" element={<BotPage />} />
                <Route path="/guide" element={<GuidePage guide="lifecycle" />} />
                <Route path="/guide/strategies" element={<GuidePage guide="strategies" />} />
                <Route path="/admin/users" element={<RequireAuth admin><UsersPage /></RequireAuth>} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Routes>
          </AuthProvider>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}
