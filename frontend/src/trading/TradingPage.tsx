import { PlusOutlined, RetweetOutlined, StopOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Badge, Button, Card, Col, List, Popconfirm, Row, Space, Statistic, Switch, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { tradingApi } from "./api";
import BotForm, { type BotFormInitial } from "./BotForm";
import RotationBotForm, { type RotationBotInitial } from "./RotationBotForm";
import { strategyName } from "../strategies";
import {
  STATUS_COLOR, botTitle, checkTimes, isRotation, localTime, nyTime, pct, pnlColor, relative, rotationRules, slotTimes, usd,
} from "./format";
import type { Bot, BotCreate, RotationBotCreate, TradingEvent, TradingStatus } from "./types";
import { usePolling } from "./usePolling";

/** Market clock + scheduler heartbeat, shown on both trading pages. Given a bot, shows that bot's next decision. */
export function MarketStatus({ status, bot }: { status: TradingStatus | null; bot?: Bot }) {
  if (!status) return null;
  // The scheduler ticks every ~30s; no tick for 3 minutes means the loop is stuck or the service is down
  const stale = !status.scheduler_last_tick || Date.now() - new Date(status.scheduler_last_tick).getTime() > 180_000;
  const t = slotTimes(status);
  const next = bot ? bot.next_decision_at : status.next_decision_at;
  return (
    <Space wrap size="middle">
      <Badge status={status.market_open ? "success" : "default"} text={status.market_open ? "Market open" : "Market closed"} />
      <Tooltip
        title={bot && isRotation(bot)
          ? `A rebalance on each month's last trading day at ${t.close} ET (earlier on half days)`
          : bot
          ? `Every ${bot.rebalance_days} trading day${bot.rebalance_days === 1 ? "" : "s"} at ${checkTimes(bot.decide_at, t)} (earlier on half days)`
          : `The soonest decision of any active bot. Bots check at ${t.open} and/or ${t.close} ET, as each is set (earlier on half days)`}
      >
        <span style={{ color: "#8b98b5" }}>
          {next ? `Next decision ${nyTime(next)} (${relative(next)})` : `No scheduled decisions while ${bot?.status}`}
        </span>
      </Tooltip>
      {!status.scheduler_enabled ? (
        <Tag color="red">Scheduler disabled</Tag>
      ) : stale ? (
        <Tooltip title="No scheduler tick in the last 3 minutes. Check `docker compose logs trading-service`.">
          <Tag color="orange">Scheduler not ticking</Tag>
        </Tooltip>
      ) : (
        <Tooltip title={`Last tick ${localTime(status.scheduler_last_tick)}`}>
          <Tag color="green">Scheduler running</Tag>
        </Tooltip>
      )}
      {status.live_trading_allowed && <Tag color="red">LIVE TRADING ENABLED</Tag>}
    </Space>
  );
}

const columns: ColumnsType<Bot> = [
  {
    title: "Bot",
    key: "bot",
    render: (_, b) => (
      <Space direction="vertical" size={0}>
        <Link to={`/trading/${b.id}`} style={{ fontWeight: 600, fontSize: 15 }}>{botTitle(b)}</Link>
        <span style={{ color: "#8b98b5", fontSize: 12 }}>{b.name}</span>
      </Space>
    ),
  },
  {
    title: "Status",
    key: "status",
    render: (_, b) => (
      <Space size={4} wrap>
        <Tag color={STATUS_COLOR[b.status]}>{b.status}</Tag>
        <Tag color={b.live ? "red" : "blue"}>{b.live ? "LIVE" : b.broker === "paper" ? "PAPER" : b.broker}</Tag>
        {b.pending_order && <Tag color="processing">order pending</Tag>}
      </Space>
    ),
  },
  { title: "Equity", dataIndex: "equity", align: "right", render: (v: number) => usd(v) },
  {
    title: "Return",
    key: "ret",
    align: "right",
    render: (_, b) => (
      <Tooltip title={`${isRotation(b) ? `Its ${b.universe?.length} symbols held equally` : "Buy & hold"} since the bot started: ${pct(b.buy_hold_return_pct)}`}>
        <span style={{ color: pnlColor(b.return_pct) }}>{pct(b.return_pct)}</span>
        <span style={{ color: "#8b98b5", fontSize: 12 }}> / B&amp;H {pct(b.buy_hold_return_pct)}</span>
      </Tooltip>
    ),
  },
  {
    title: "Position",
    key: "pos",
    render: (_, b) =>
      isRotation(b) ? (
        b.holdings.length ? (
          <span>
            {b.holdings.map((h) => h.symbol).join(", ")} <span style={{ color: pnlColor(b.unrealized_pnl) }}>({usd(b.unrealized_pnl)})</span>
          </span>
        ) : (
          <span style={{ color: "#8b98b5" }}>cash</span>
        )
      ) : b.shares > 0 ? (
        <span>
          {b.shares} sh @ {usd(b.entry_price)}{" "}
          <span style={{ color: pnlColor(b.unrealized_pnl) }}>({usd(b.unrealized_pnl)})</span>
        </span>
      ) : (
        <span style={{ color: "#8b98b5" }}>flat</span>
      ),
  },
  { title: "Last price", key: "last_price", align: "right", render: (_, b) => (isRotation(b) ? "—" : usd(b.last_price)) },
  {
    title: "Strategy",
    key: "params",
    render: (_, b) => (isRotation(b) ? (
      <span style={{ fontSize: 12, color: "#8b98b5" }}>Momentum rotation · {rotationRules(b)} · month ends at {slotTimes().close}</span>
    ) : (
      <span style={{ fontSize: 12, color: "#8b98b5" }}>
        {strategyName(b.strategy)} · conf ≥ {b.min_confidence} · every {b.rebalance_days}d at {checkTimes(b.decide_at, slotTimes())} · stop {+(b.stop_pct * 100).toFixed(1)}% / tgt {+(b.target_pct * 100).toFixed(1)}%
      </span>
    )),
  },
  { title: "Last decision", key: "last", render: (_, b) => (b.last_decision_at ? nyTime(b.last_decision_at) : b.last_decision_date ?? "—") },
  { title: "Next decision", dataIndex: "next_decision_at", render: (v: string | null) => (v ? nyTime(v) : "—") },
];

const LEVEL_COLOR: Record<string, string> = { info: "default", warning: "orange", error: "red" };

/** Recent audit events across all bots. */
function ActivityFeed({ events, bots }: { events: TradingEvent[]; bots: Bot[] }) {
  const symbol = (id: number | null) => {
    const bot = id == null ? undefined : bots.find((b) => b.id === id);
    return id == null ? "system" : bot ? botTitle(bot) : `#${id}`;
  };
  return (
    <Card title="Recent activity" size="small" style={{ marginTop: 16 }}>
      <List
        size="small"
        dataSource={events.slice(0, 15)}
        locale={{ emptyText: "Nothing yet. Create a bot to get started." }}
        renderItem={(e) => (
          <List.Item>
            <Space align="start">
              <span style={{ color: "#8b98b5", whiteSpace: "nowrap", fontSize: 12 }}>{localTime(e.created_at)}</span>
              <Tag color={LEVEL_COLOR[e.level]}>{symbol(e.bot_id)}</Tag>
              <span>{e.message}</span>
            </Space>
          </List.Item>
        )}
      />
    </Card>
  );
}

/** Overview of every trading bot: portfolio totals, the bot table, the kill switch and a live activity feed. */
export default function TradingPage() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const [showArchived, setShowArchived] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [initial, setInitial] = useState<BotFormInitial>({});
  const [rotationOpen, setRotationOpen] = useState(false);
  const [rotationInitial, setRotationInitial] = useState<RotationBotInitial>({});

  const bots = usePolling(() => tradingApi.listBots(showArchived), 15_000, [showArchived]);
  const status = usePolling(tradingApi.status, 30_000);
  const events = usePolling(tradingApi.allEvents, 15_000);

  // Arriving from the backtest page's "Trade this strategy" (or the rotation page's "Paper trade this"): open the form pre-filled
  useEffect(() => {
    const state = location.state as { prefill?: BotFormInitial; rotationPrefill?: RotationBotInitial } | null;
    if (state?.prefill) {
      setInitial(state.prefill);
      setFormOpen(true);
    } else if (state?.rotationPrefill) {
      setRotationInitial(state.rotationPrefill);
      setRotationOpen(true);
    } else return;
    navigate(location.pathname, { replace: true, state: null }); // don't reopen on refresh
  }, [location, navigate]);

  const list = bots.data ?? [];
  const totals = useMemo(() => {
    const running = list.filter((b) => b.status !== "archived");
    const allocated = running.reduce((s, b) => s + b.allocated_cash, 0);
    const equity = running.reduce((s, b) => s + b.equity, 0);
    return { allocated, equity, pnl: equity - allocated, active: running.filter((b) => b.status === "active").length, count: running.length };
  }, [list]);

  const create = async (body: BotCreate | object) => {
    const bot = await tradingApi.createBot(body as BotCreate);
    message.success(`Bot created: ${bot.name}`);
    setFormOpen(false);
    navigate(`/trading/${bot.id}`);
  };

  const createRotation = async (body: RotationBotCreate) => {
    const bot = await tradingApi.createRotationBot(body);
    message.success(`Bot created: ${bot.name}`);
    setRotationOpen(false);
    navigate(`/trading/${bot.id}`);
  };

  const halt = async () => {
    try {
      const r = await tradingApi.halt();
      message.warning(`Paused ${r.paused} bot(s). Open positions keep their stop-loss.`);
      bots.reload();
      events.reload();
    } catch (e) {
      message.error(String(e));
    }
  };

  return (
    <>
      <Row justify="space-between" align="middle" gutter={[16, 12]} style={{ marginBottom: 16 }}>
        <Col>
          <Typography.Title level={4} style={{ margin: 0 }}>Trading bots</Typography.Title>
          <MarketStatus status={status.data} />
        </Col>
        <Col>
          <Space wrap>
            <Popconfirm
              title="Pause ALL bots?"
              description="No new decisions until you resume each bot. Stop-loss/target keep protecting open positions."
              okText="Pause all"
              okButtonProps={{ danger: true }}
              onConfirm={halt}
            >
              <Button danger icon={<StopOutlined />} disabled={totals.active === 0}>Halt all</Button>
            </Popconfirm>
            <Tooltip title="Holds the strongest few of a group of symbols, rebalanced at each month's end (see the Rotation page)">
              <Button icon={<RetweetOutlined />} onClick={() => { setRotationInitial({}); setRotationOpen(true); }}>
                New rotation bot
              </Button>
            </Tooltip>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { setInitial({}); setFormOpen(true); }}>
              New bot
            </Button>
          </Space>
        </Col>
      </Row>

      {(bots.error || status.error) && (
        <Alert type="error" showIcon style={{ marginBottom: 16 }}
          message="Can't reach trading-service"
          description={`${bots.error ?? status.error}. Is it running? docker compose up -d trading-service`} />
      )}

      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Capital allocated" value={usd(totals.allocated, 0)} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Current equity" value={usd(totals.equity, 0)} /></Card></Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Total P&L" value={usd(totals.pnl)} valueStyle={{ color: pnlColor(totals.pnl) }} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Active bots" value={totals.active} suffix={`/ ${totals.count}`} />
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        style={{ marginTop: 16 }}
        title="Bots"
        extra={<Space><span style={{ color: "#8b98b5" }}>Show archived</span><Switch size="small" checked={showArchived} onChange={setShowArchived} /></Space>}
      >
        <Table<Bot>
          rowKey="id"
          columns={columns}
          dataSource={list}
          loading={bots.loading && !bots.data}
          pagination={false}
          size="middle"
          scroll={{ x: 900 }}
          onRow={(b) => ({ onClick: (e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/trading/${b.id}`); }, style: { cursor: "pointer" } })}
          locale={{ emptyText: "No bots yet. Run a backtest you trust, then click “Trade this strategy”, or create one with New bot." }}
        />
      </Card>

      <ActivityFeed events={events.data ?? []} bots={list} />

      <BotForm
        open={formOpen}
        editing={false}
        initial={initial}
        brokers={status.data?.brokers ?? [{ name: "paper", label: "Paper (built-in simulator)", live: false, available: true, reason: "" }]}
        times={slotTimes(status.data)}
        onCancel={() => setFormOpen(false)}
        onSubmit={create}
      />
      <RotationBotForm
        open={rotationOpen}
        initial={rotationInitial}
        brokers={status.data?.brokers ?? [{ name: "paper", label: "Paper (built-in simulator)", live: false, available: true, reason: "" }]}
        onCancel={() => setRotationOpen(false)}
        onSubmit={createRotation}
      />
    </>
  );
}
