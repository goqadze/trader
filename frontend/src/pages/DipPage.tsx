import { PlusOutlined, RocketOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, Empty, Row, Space, Statistic, Tabs, Tag, Tooltip, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import DipBacktestForm from "../dip/DipBacktestForm";
import DipBotForm, { type DipBotInitial } from "../dip/DipBotForm";
import { SignalsList, dipRules } from "../dip/DipParts";
import DipResultView from "../dip/DipResultView";
import type { DipBotCreate, DipRules } from "../dip/types";
import { useDip } from "../dip/useDip";
import { isListedEtf } from "../symbols";
import { tradingApi } from "../trading/api";
import { STATUS_COLOR, isDip, nyTime, pct, pnlColor, usd } from "../trading/format";
import type { Bot } from "../trading/types";
import { usePolling } from "../trading/usePolling";

const MUTED = "#8b98b5";
const TAB_KEY = "dip:tab";

function savedTab(): string {
  try {
    return localStorage.getItem(TAB_KEY) ?? "bots";
  } catch {
    return "bots";
  }
}

/** One dip bot at a glance: equity, what it holds, what is in the buy zone right now. */
function BotCard({ bot }: { bot: Bot }) {
  const zone = bot.watchlist.filter((w) => w.in_zone && w.status === "watching" && !w.held).map((w) => w.symbol);
  const blacklisted = bot.watchlist.filter((w) => w.status === "blacklisted").map((w) => w.symbol);
  return (
    <Card
      size="small"
      title={<Link to={`/trading/${bot.id}`}>{bot.name}</Link>}
      extra={
        <Space size={4}>
          <Tag color={STATUS_COLOR[bot.status]}>{bot.status}</Tag>
          <Tag color="blue">{bot.broker === "paper" ? "PAPER" : bot.broker}</Tag>
        </Space>
      }
    >
      <Row gutter={12}>
        <Col span={8}><Statistic title="Equity" value={usd(bot.equity, 0)} valueStyle={{ fontSize: 18 }} /></Col>
        <Col span={8}>
          <Tooltip title={`Its starting ${bot.universe?.length ?? 0} symbols held equally: ${pct(bot.buy_hold_return_pct)}`}>
            <Statistic title="Return" value={pct(bot.return_pct)} valueStyle={{ fontSize: 18, color: pnlColor(bot.return_pct) }} />
          </Tooltip>
        </Col>
        <Col span={8}><Statistic title="Holding" value={`${bot.holdings.length} / ${bot.max_positions}`} valueStyle={{ fontSize: 18 }} /></Col>
      </Row>
      <div style={{ fontSize: 12, color: MUTED, marginTop: 8 }}>{dipRules(bot)}</div>
      <div style={{ marginTop: 6 }}>
        {bot.holdings.map((h) => (
          <Tag key={h.symbol} color="blue">{h.symbol} <span style={{ color: pnlColor(h.unrealized_pnl) }}>{pct((h.unrealized_pnl / h.cost_basis) * 100, 1)}</span></Tag>
        ))}
        {zone.map((s) => <Tag key={s} color="geekblue">{s} buy zone</Tag>)}
        {blacklisted.map((s) => <Tag key={s} color="red">{s} blacklisted</Tag>)}
      </div>
      <div style={{ fontSize: 12, color: MUTED, marginTop: 6 }}>
        Watching {bot.watchlist.length} · next check {bot.next_decision_at ? nyTime(bot.next_decision_at) : "—"} · news {bot.news ? "on" : "off"}
      </div>
    </Card>
  );
}

/** The dip buyer's home: your dip bots and their signals (the recommendations), and the backtest with its practice and
 *  exam windows. */
export default function DipPage() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const { runs, error, starting, start } = useDip();
  const [tab, setTab] = useState(savedTab);
  const [formOpen, setFormOpen] = useState(false);
  const [initial, setInitial] = useState<DipBotInitial>({});
  const bots = usePolling(() => tradingApi.listBots(false), 15_000);
  const status = usePolling(tradingApi.status, 60_000);
  const signals = usePolling(() => tradingApi.signals(0, 300), 15_000);
  const dipBots = (bots.data ?? []).filter(isDip);

  // The trading page's "New dip bot" lands here with the form open
  useEffect(() => {
    const state = location.state as { newDipBot?: boolean } | null;
    if (!state?.newDipBot) return;
    setInitial({});
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

  /** Opens the new dip bot form with exactly the rules and watchlist that were tested (the bot keeps its own safety
   *  defaults: a backtest runs without the drawdown breaker unless asked). */
  const paperTrade = () => {
    if (!cfg) return;
    const rules: DipRules = {
      interval: cfg.interval, drop_pct: cfg.drop_pct, lookback: cfg.lookback, lookback_unit: cfg.lookback_unit, drop_from: cfg.drop_from,
      target_mode: cfg.target_mode, rise_pct: cfg.rise_pct, stop_pct: cfg.stop_pct, max_positions: cfg.max_positions,
      max_hold_days: cfg.max_hold_days, news: cfg.news, trend_filter: cfg.trend_filter, rebound: cfg.rebound, rebound_pct: cfg.rebound_pct,
    };
    setInitial({
      ...rules, symbols: cfg.symbols, ...(cfg.initial_cash && { allocated_cash: cfg.initial_cash }),
      ...(cfg.slippage_pct != null && { slippage_pct: cfg.slippage_pct }),
    });
    setFormOpen(true);
  };

  const create = async (body: DipBotCreate) => {
    const bot = await tradingApi.createDipBot(body);
    message.success(`Bot created: ${bot.name}`);
    setFormOpen(false);
    navigate(`/trading/${bot.id}`);
  };

  const backtest = (
    <Row gutter={[16, 16]}>
      <Col xs={24} md={9} lg={7}>
        <DipBacktestForm onRun={start} starting={starting} />
      </Col>
      <Col xs={24} md={15} lg={17}>
        {error && <Alert type="error" showIcon closable message={error} style={{ marginBottom: 16 }} />}
        {stocks.length > 0 && done && (
          <Alert
            type="warning"
            showIcon
            closable
            style={{ marginBottom: 16 }}
            message="Hindsight warning: these are today's big winners"
            description={`${stocks.slice(0, 8).join(", ")}${stocks.length > 8 ? "…" : ""} are picked knowing how the past turned out. Their dips recovered because they
              went on to win, which flatters a dip buyer (and holding them even more). Try sector ETFs too.`}
          />
        )}
        {runs.practice || runs.exam ? (
          <>
            {done && (
              <Space wrap style={{ marginBottom: 16 }}>
                <Button type="primary" icon={<RocketOutlined />} onClick={paperTrade}>Paper trade these rules</Button>
                <span style={{ color: MUTED, fontSize: 12 }}>A dip bot with these rules and this watchlist on a paper account. You can change both while it runs.</span>
              </Space>
            )}
            <DipResultView title="Practice" run={runs.practice} />
            <DipResultView title="Exam" run={runs.exam} />
          </>
        ) : (
          <Card>
            <Empty
              description={
                <span style={{ color: MUTED }}>
                  "Buy the dip" bets that a sharp fall is an overreaction that comes back. Test it here first: the practice years, then the
                  exam years it never saw, against simply holding the same symbols.
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
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { setInitial({}); setFormOpen(true); }}>New dip bot</Button>
          <span style={{ color: MUTED, fontSize: 12 }}>Every check posts its signals here and to the bell at the top (pop-up, desktop notification, sound, read aloud).</span>
        </Space>
        {bots.error && <Alert type="error" showIcon message={`Can't reach trading-service: ${bots.error}`} style={{ marginBottom: 12 }} />}
        {dipBots.length === 0 ? (
          <Card>
            <Empty description={<span style={{ color: MUTED }}>No dip bots yet. Backtest your rules first, then “Paper trade these rules”, or start one with New dip bot.</span>} />
          </Card>
        ) : (
          <Row gutter={[12, 12]}>
            {dipBots.map((b) => <Col key={b.id} xs={24} xl={12}><BotCard bot={b} /></Col>)}
          </Row>
        )}
      </Col>
      <Col xs={24} lg={10}>
        <Card size="small" title="Signals (recommendations)">
          <SignalsList signals={signals.data ?? []} />
        </Card>
      </Col>
    </Row>
  );

  return (
    <>
      <Typography.Title level={4} style={{ marginTop: 0 }}>Dip buyer</Typography.Title>
      <Tabs
        activeKey={tab}
        onChange={switchTab}
        items={[
          { key: "bots", label: `Bots & signals${dipBots.length ? ` (${dipBots.length})` : ""}`, children: live },
          { key: "backtest", label: "Backtest", children: backtest },
        ]}
      />
      <DipBotForm
        open={formOpen}
        initial={initial}
        brokers={status.data?.brokers ?? [{ name: "paper", label: "Paper (built-in simulator)", live: false, available: true, reason: "" }]}
        onCancel={() => setFormOpen(false)}
        onSubmit={create}
      />
    </>
  );
}
