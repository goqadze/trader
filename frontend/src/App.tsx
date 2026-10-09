import { AppstoreOutlined, ApartmentOutlined, DownOutlined, ExportOutlined, FundOutlined, MenuOutlined, ReadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, ConfigProvider, Drawer, Dropdown, Grid, Layout, Menu, theme, type MenuProps } from "antd";
import { useState } from "react";
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AccountMenu, UsersNavLabel } from "./auth/AccountMenu";
import { AuthProvider, RequireAuth, useAuth } from "./auth/AuthContext";
import { LoginPage, SignupPage } from "./auth/AuthPages";
import UsersPage from "./auth/UsersPage";
import SignalBell from "./dip/SignalBell";
import BacktestPage from "./pages/BacktestPage";
import ComparePage from "./pages/ComparePage";
import DipPage from "./pages/DipPage";
import EmailAlertsPage from "./pages/EmailAlertsPage";
import RotationPage from "./pages/RotationPage";
import ScanPage from "./pages/ScanPage";
import GuidePage from "./pages/GuidePage";
import BotPage from "./trading/BotPage";
import TradingPage from "./trading/TradingPage";

// External dashboards for observability, API docs and the database browser: their ports from docker-compose, on the
// host this page came from. That's localhost on the machine running the stack (or through an SSH tunnel), and the
// server's Tailscale name on a server, where `tailscale serve` publishes the same ports (DEPLOY.md).
const at = (port: number, path = "") => `${window.location.protocol}//${window.location.hostname}:${port}${path}`;
const MONITORING = [
  { label: "Langfuse (LLM traces)", href: at(3000) },
  { label: "GlitchTip (errors)", href: at(8082) },
];
const API_DOCS = [
  { label: "Backtest API", href: at(8001, "/docs") },
  { label: "Decision API", href: at(8000, "/docs") },
  { label: "Trading API", href: at(8002, "/docs") },
];
const DATABASES = [{ label: "pgAdmin (tables & data)", href: at(5050) }];

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
      { key: "/guide/architecture", icon: <ApartmentOutlined />, label: <Link to="/guide/architecture">Architecture & Stack</Link> },
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

/** Top bar: app name, page navigation, the Resources dropdown, and the account menu. Under 1200px the pages move into
 *  a drawer behind a menu button (the bar can't show them all), and on a phone the buttons on the right lose their
 *  words, so the bar fits. */
function Header() {
  const { pathname } = useLocation();
  const admin = useAuth().user?.role === "admin";
  const screens = Grid.useBreakpoint(); // undefined before the first measure: the desktop bar
  const drawerNav = screens.xl === false; // under 1200px
  const mobile = screens.md === false; // under 768px
  const [drawer, setDrawer] = useState(false);
  const pages: MenuProps["items"] = [
    { key: "backtest", label: <Link to="/">Backtest</Link> },
    { key: "compare", label: <Link to="/compare">Compare strategies</Link> },
    { key: "scan", label: <Link to="/scan">Scan</Link> },
    { key: "rotation", label: <Link to="/rotation">Rotation</Link> },
    { key: "dip", label: <Link to="/dip">Dip buyer</Link> },
    { key: "trading", label: <Link to="/trading">Live trading</Link> },
    ...(admin ? [{ key: "users", label: <Link to="/admin/users"><UsersNavLabel /></Link> }] : []),
  ];
  const current = pathname.startsWith("/trading")
    ? "trading"
    : pathname.startsWith("/admin/users")
      ? "users"
      : pathname === "/compare"
        ? "compare"
        : pathname === "/scan"
          ? "scan"
          : pathname === "/rotation"
            ? "rotation"
          : pathname === "/dip"
            ? "dip"
        : pathname === "/"
          ? "backtest"
          : "";
  const selected = current ? [current] : [];
  return (
    // overflow hidden: the horizontal menu measures the pages that don't fit off to the right, which would
    // otherwise widen the whole page
    <Layout.Header style={{ display: "flex", alignItems: "center", gap: mobile ? 4 : 16, paddingInline: mobile ? 8 : drawerNav ? 24 : 50, overflow: "hidden" }}>
      {drawerNav && <Button type="text" icon={<MenuOutlined />} aria-label="Pages" onClick={() => setDrawer(true)} style={{ color: "#e6ebf5" }} />}
      <span style={{ fontWeight: 600, fontSize: 18, whiteSpace: "nowrap", marginRight: drawerNav ? "auto" : undefined }}>📈 Trading</span>
      {!drawerNav && (
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={selected}
          style={{ flex: "1 1 auto", minWidth: 0, background: "transparent", borderBottom: "none" }}
          items={pages}
        />
      )}
      <Dropdown menu={{ items: resources(admin), selectedKeys: [pathname] }} trigger={["click"]} placement="bottomRight">
        <Button type="text" icon={<AppstoreOutlined />} aria-label="Resources" style={{ color: "#8b98b5" }}>
          {!mobile && <>Resources <DownOutlined style={{ fontSize: 10 }} /></>}
        </Button>
      </Dropdown>
      <SignalBell />
      <AccountMenu compact={mobile} />
      {drawerNav && (
        <Drawer title="📈 Trading" placement="left" width={260} open={drawer} onClose={() => setDrawer(false)} styles={{ body: { padding: 0 } }}>
          <Menu mode="inline" selectedKeys={selected} items={pages} onClick={() => setDrawer(false)} style={{ borderInlineEnd: "none", background: "transparent" }} />
        </Drawer>
      )}
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
                <Route path="/scan" element={<ScanPage />} />
                <Route path="/rotation" element={<RotationPage />} />
                <Route path="/dip" element={<DipPage />} />
                <Route path="/trading" element={<TradingPage />} />
                <Route path="/trading/:id" element={<BotPage />} />
                <Route path="/alerts" element={<EmailAlertsPage />} />
                <Route path="/guide" element={<GuidePage guide="lifecycle" />} />
                <Route path="/guide/strategies" element={<GuidePage guide="strategies" />} />
                <Route path="/guide/architecture" element={<GuidePage guide="architecture" />} />
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
