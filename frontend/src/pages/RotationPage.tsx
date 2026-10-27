import { PlusOutlined, RocketOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, Empty, Row, Space, Statistic, Tabs, Tag, Tooltip, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ROTATION_KIT, momentumText } from "../rotation/kit";
import RotationForm from "../rotation/RotationForm";
import RotationResult from "../rotation/RotationResult";
import type { RotationPresetConfig } from "../rotation/types";
import { useRotation } from "../rotation/useRotation";
import TestResults from "../studies/TestResults";
import { isListedEtf } from "../symbols";
import { tradingApi } from "../trading/api";
import { STATUS_COLOR, isRotation, nyTime, pct, pnlColor, usd } from "../trading/format";
import RotationBotForm, { type RotationBotInitial } from "../trading/RotationBotForm";
import type { Bot, Decision, RotationBotCreate } from "../trading/types";
import { usePolling } from "../trading/usePolling";

const MUTED = "#8b98b5";
const TAB_KEY = "rotation:tab";

function savedTab(): string {
  try {
    return localStorage.getItem(TAB_KEY) ?? "bots";
  } catch {
    return "bots";
  }
}

/** One rotation bot at a glance: equity against holding its universe, what it holds, when it rebalances next. */
function BotCard({ bot }: { bot: Bot }) {
  return (
    <Card
      size="small"
      title={<Link to={`/trading/${bot.id}`}>{bot.name}</Link>}
      extra={
        <Space size={4}>
          <Tag color={STATUS_COLOR[bot.status]}>{bot.status}</Tag>
          <Tag color={bot.live ? "red" : "blue"}>{bot.live ? "LIVE — real money" : bot.broker === "paper" ? "PAPER" : bot.broker}</Tag>
        </Space>
      }
    >
      <Row gutter={12}>
        <Col span={8}><Statistic title="Equity" value={usd(bot.equity, 0)} valueStyle={{ fontSize: 18 }} /></Col>
        <Col span={8}>
          <Tooltip title={`Its ${bot.universe?.length ?? 0} symbols held equally since it started: ${pct(bot.buy_hold_return_pct)}`}>
            <Statistic title="Return" value={pct(bot.return_pct)} valueStyle={{ fontSize: 18, color: pnlColor(bot.return_pct) }} />
          </Tooltip>
        </Col>
        <Col span={8}><Statistic title="Holding" value={`${bot.holdings.length} / ${bot.top_n ?? 0}`} valueStyle={{ fontSize: 18 }} /></Col>
      </Row>
      <div style={{ fontSize: 12, color: MUTED, marginTop: 8 }}>
        Top {bot.top_n} of {bot.universe?.length ?? 0} · {momentumText({ lookback_months: bot.lookback_months ?? 12, skip_months: bot.skip_months ?? 0 })} momentum
        · {bot.abs_filter ? "rising only" : "rising or not"}
      </div>
      <div style={{ marginTop: 6 }}>
        {bot.holdings.map((h) => (
          <Tag key={h.symbol} color="blue">{h.symbol} <span style={{ color: pnlColor(h.unrealized_pnl) }}>{pct((h.unrealized_pnl / h.cost_basis) * 100, 1)}</span></Tag>
        ))}
        {!bot.holdings.length && <span style={{ fontSize: 12, color: MUTED }}>Holding cash</span>}
      </div>
      <div style={{ fontSize: 12, color: MUTED, marginTop: 6 }}>
        Next rebalance {bot.next_decision_at ? nyTime(bot.next_decision_at) : "— (paused)"}
      </div>
    </Card>
  );
}

/** The rotation bots' latest decisions, newest first: what each rebalance held, bought and sold. */
function Rebalances({ bots }: { bots: Bot[] }) {
  const ids = bots.map((b) => b.id).join(",");
  const decisions = usePolling(async () => {
    const lists = await Promise.all(bots.map(async (b) => (await tradingApi.decisions(b.id)).slice(0, 6).map((d) => ({ ...d, bot: b }))));
    return lists.flat().sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the bots' ids decide what to ask for
  }, 60_000, [ids]);
  const rows: (Decision & { bot: Bot })[] = decisions.data ?? [];
  if (!rows.length) {
    return <span style={{ color: MUTED, fontSize: 12 }}>No rebalances yet: a bot rebalances when it starts, then at each month's last close.</span>;
  }
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      {rows.map((d) => (
        <div key={d.id} style={{ fontSize: 13 }}>
          <div style={{ color: MUTED, fontSize: 12 }}>
            {nyTime(d.created_at)} · <Link to={`/trading/${d.bot.id}`}>{d.bot.name}</Link> · <Tag style={{ marginInlineEnd: 0 }}>{d.action}</Tag>
          </div>
          <div>{d.reasoning}</div>
        </div>
      ))}
    </Space>
  );
}

/** The momentum rotation's home: your rotation bots and their rebalances, the backtest with its practice and exam
 *  windows, and the test batches (parameter sweeps). */
export default function RotationPage() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const { runs, error, starting, start } = useRotation();
  const [tab, setTab] = useState(savedTab);
  const [formOpen, setFormOpen] = useState(false);
  const [initial, setInitial] = useState<RotationBotInitial>({});
  // The form's tested setup, if any: a Test results test, or (preset) the saved setup that keeps one
  const [fromTest, setFromTest] = useState<{ id: number; name: string; preset?: boolean } | null>(null);
  const [loadRequest, setLoadRequest] = useState<{ config: RotationPresetConfig; seq: number } | null>(null);
  const bots = usePolling(() => tradingApi.listBots(), 30_000);
  const status = usePolling(tradingApi.status, 60_000);
  const rotationBots = (bots.data ?? []).filter(isRotation);

  // The trading page's "New rotation bot" lands here with the form open
  useEffect(() => {
    const state = location.state as { newRotationBot?: boolean } | null;
    if (!state?.newRotationBot) return;
    setInitial({});
    setFromTest(null);
    setFormOpen(true);
    navigate(location.pathname, { replace: true, state: null });
  }, [location, navigate]);

  const switchTab = (k: string) => {
    setTab(k);
    try {
      localStorage.setItem(TAB_KEY, k);
    } catch {
      /* not remembered */
    }
  };

  const cfg = runs.exam?.config ?? runs.practice?.config;
  const stocks = (cfg?.symbols ?? []).filter((s) => !isListedEtf(s));
  const done = [runs.practice, runs.exam].some((r) => r?.result);

  /** Opens the new rotation bot form with exactly the rules and universe that were tested. */
  const paperTrade = () => {
    if (!cfg) return;
    setInitial({
      universe: cfg.symbols, top_n: cfg.top_n, lookback_months: cfg.lookback_months, skip_months: cfg.skip_months, abs_filter: cfg.abs_filter,
      ...(cfg.slippage_pct != null && { slippage_pct: cfg.slippage_pct }),
      ...(cfg.initial_cash != null && { allocated_cash: cfg.initial_cash }),
      ...(cfg.fractional != null && { fractional: cfg.fractional }),
    });
    setFromTest(null);
    setFormOpen(true);
  };

  /** The Test results tab's "Load into the backtest form": switch to the backtest with that setup filled in. */
  const loadTest = (config: RotationPresetConfig) => {
    setLoadRequest((r) => ({ config, seq: (r?.seq ?? 0) + 1 }));
    switchTab("backtest");
    message.info("Loaded into the backtest form: press Run rotation");
  };

  /** The Test results tab's "Start a bot": the new rotation bot form with that setup's rules, universe and capital. The
   *  bot keeps a copy of the test (its name and results: Tested setup on its page). */
  const startTest = (config: RotationPresetConfig, name: string, testId: number) => {
    setInitial({
      name, universe: config.symbols, top_n: config.top_n, lookback_months: config.lookback_months, skip_months: config.skip_months,
      abs_filter: config.abs_filter, fractional: config.fractional, ...(config.initial_cash && { allocated_cash: config.initial_cash }),
    });
    setFromTest({ id: testId, name });
    setFormOpen(true);
  };

  const create = async (body: RotationBotCreate) => {
    const link = fromTest && (fromTest.preset ? { setup_preset_id: fromTest.id } : { setup_test_id: fromTest.id });
    const bot = await tradingApi.createRotationBot({ ...body, ...link });
    message.success(`Bot created: ${bot.name}`);
    setFormOpen(false);
    navigate(`/trading/${bot.id}`);
  };

  const backtest = (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={9} lg={7}>
        <RotationForm onRun={start} starting={starting} loadRequest={loadRequest} />
      </Col>
      <Col xs={24} md={15} lg={17}>
        {error && <Alert type="error" showIcon closable message={error} style={{ marginBottom: 16 }} />}
        {stocks.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="Hindsight warning: this universe has individual stocks"
            description={`${stocks.slice(0, 8).join(", ")}${stocks.length > 8 ? "…" : ""} are companies picked today (today's biggest, or an article's picks):
              testing them on past years selects yesterday's winners in hindsight (survivorship bias), which flatters both the rotation and
              holding them. Sector ETFs don't have that problem.`}
          />
        )}
        {runs.practice || runs.exam ? (
          <>
            {done && (
              <Space wrap style={{ marginBottom: 16 }}>
                <Button type="primary" icon={<RocketOutlined />} onClick={paperTrade}>Paper trade this rotation</Button>
                <span style={{ color: MUTED, fontSize: 12 }}>
                  A bot with these rules on a paper account: it rebalances at each month's last close, so give it months, not days.
                </span>
              </Space>
            )}
            <RotationResult title="Practice" run={runs.practice} />
            <RotationResult title="Exam" run={runs.exam} />
          </>
        ) : (
          <Card>
            <Empty
              description={
                <span style={{ color: MUTED }}>
                  Momentum is one of the best-documented effects in markets: what rose over the past year tends to keep rising for a
                  while. Test it on the 11 sector ETFs first: the practice years, then the exam years it never saw.
                </span>
              }
            />
          </Card>
        )}
      </Col>
    </Row>
  );

  const live = (
    <Row gutter={[16, 16]}>
      <Col xs={24} lg={14}>
        <Space style={{ marginBottom: 12 }} wrap>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { setInitial({}); setFromTest(null); setFormOpen(true); }}>New rotation bot</Button>
          <span style={{ color: MUTED, fontSize: 12 }}>Each bot holds the strongest few of its universe and rebalances at each month's last close.</span>
        </Space>
        {bots.error && <Alert type="error" showIcon message={`Can't reach trading-service: ${bots.error}`} style={{ marginBottom: 12 }} />}
        {rotationBots.length === 0 ? (
          <Card>
            <Empty description={<span style={{ color: MUTED }}>No rotation bots yet. Backtest a rotation first, then “Paper trade this rotation”, or start one with New rotation bot.</span>} />
          </Card>
        ) : (
          <Row gutter={[12, 12]}>
            {rotationBots.map((b) => <Col key={b.id} xs={24} xl={12}><BotCard bot={b} /></Col>)}
          </Row>
        )}
      </Col>
      <Col xs={24} lg={10}>
        <Card size="small" title="Rebalances">
          <Rebalances bots={rotationBots} />
        </Card>
      </Col>
    </Row>
  );

  return (
    <>
      <Typography.Title level={4} style={{ marginTop: 0 }}>Momentum Rotation</Typography.Title>
      <Tabs
        activeKey={tab}
        onChange={switchTab}
        items={[
          { key: "bots", label: `Bots${rotationBots.length ? ` (${rotationBots.length})` : ""}`, children: live },
          { key: "backtest", label: "Backtest", children: backtest },
          { key: "tests", label: "Test results", children: <TestResults kit={ROTATION_KIT} onLoad={loadTest} onStartBot={startTest} /> },
        ]}
      />
      <RotationBotForm
        open={formOpen}
        initial={initial}
        fromSetup={fromTest?.name}
        onUnlink={() => setFromTest(null)}
        onLink={(p) => setFromTest({ id: p.id, name: p.setup?.name ?? p.name, preset: true })}
        brokers={status.data?.brokers ?? [{ name: "paper", label: "Paper (built-in simulator)", live: false, available: true, reason: "" }]}
        onCancel={() => setFormOpen(false)}
        onSubmit={create}
      />
    </>
  );
}
