import { DownOutlined, FallOutlined, PlusOutlined, RetweetOutlined, StopOutlined } from "@ant-design/icons";
import {
  Alert, App as AntApp, Badge, Button, Card, Col, Dropdown, Grid, List, Row, Segmented, Select, Space, Statistic, Switch, Table, Tag, Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { SortOrder } from "antd/es/table/interface";
import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { tradingApi } from "./api";
import BotForm, { type BotFormInitial } from "./BotForm";
import { useConfirm } from "./confirm";
import { SectorTags } from "./sectors";
import { dipRules } from "../dip/DipParts";
import { strategyName } from "../strategies";
import {
  STATUS_COLOR, botTitle, checkTimes, holdsMany, isDip, isRotation, localTime, nyTime, pct, pnlColor, relative, rotationRules, slotTimes, usd,
} from "./format";
import type { AccountInfo, Bot, BotCreate, TradingEvent, TradingStatus } from "./types";
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
          : bot && isDip(bot)
          ? bot.interval === "1d"
            ? `A check of every watched symbol once a day at ${t.close} ET (earlier on half days)`
            : `A check of every watched symbol at the end of each ${bot.interval} bar from the 9:30 open, until the close`
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
    // The number reports, emails and Claude call a bot by ("#7"); the same as in its page's address
    title: "ID",
    key: "id",
    width: 70,
    sorter: (a, b) => a.id - b.id,
    sortDirections: ["ascend", "descend"],
    render: (_, b) => <Link to={`/trading/${b.id}`} style={{ color: "#8b98b5", whiteSpace: "nowrap" }}>#{b.id}</Link>,
  },
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
    // Which parts of the market it trades (filterable: the filter's choices come from the bots listed, see below)
    title: "Sectors",
    key: "sectors",
    width: 150,
    render: (_, b) => <SectorTags sectors={b.sectors} />,
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
    sorter: (a, b) => a.return_pct - b.return_pct,
    sortDirections: ["descend", "ascend"],
    render: (_, b) => (
      <Tooltip title={`${isRotation(b) ? `Its ${b.universe?.length} symbols held equally` : isDip(b) ? `Its starting ${b.universe?.length} symbols held equally` : "Buy & hold"} since the bot started: ${pct(b.buy_hold_return_pct)}`}>
        <span style={{ color: pnlColor(b.return_pct) }}>{pct(b.return_pct)}</span>
        <span style={{ color: "#8b98b5", fontSize: 12 }}> / B&amp;H {pct(b.buy_hold_return_pct)}</span>
      </Tooltip>
    ),
  },
  {
    title: "Position",
    key: "pos",
    // By the open positions' gain or loss in dollars (flat or all cash: $0)
    sorter: (a, b) => a.unrealized_pnl - b.unrealized_pnl,
    sortDirections: ["descend", "ascend"],
    render: (_, b) =>
      holdsMany(b) ? (
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
  { title: "Last price", key: "last_price", align: "right", render: (_, b) => (holdsMany(b) ? "—" : usd(b.last_price)) },
  {
    title: "Strategy",
    key: "params",
    render: (_, b) => (isRotation(b) ? (
      <span style={{ fontSize: 12, color: "#8b98b5" }}>Momentum rotation · {rotationRules(b)} · month ends at {slotTimes().close}</span>
    ) : isDip(b) ? (
      <span style={{ fontSize: 12, color: "#8b98b5" }}>Dip buyer on {b.watchlist.length} · {dipRules(b)}{b.news ? " · news on" : ""}</span>
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

// The overview's broker filter: one account's bots (or the simulator's), with their totals and activity; remembered in
// this browser only
const BROKER_KEY = "trading.brokerFilter";
const BROKER_ORDER = ["alpaca-live", "alpaca-paper", "paper"];
const BROKER_LABEL: Record<string, string> = { "alpaca-live": "Alpaca live", "alpaca-paper": "Alpaca paper", paper: "Simulator" };

function rememberedBroker(): string {
  try {
    return localStorage.getItem(BROKER_KEY) ?? "all";
  } catch {
    return "all";
  }
}

function rememberBroker(broker: string) {
  try {
    localStorage.setItem(BROKER_KEY, broker);
  } catch {
    /* private window or blocked storage: the filter still works, it just won't be remembered */
  }
}

// The bot table's sort (ID, Return or Position, by a click on the column), remembered in this browser too
const SORT_KEY = "trading.botSort";
type BotSort = { key: string; order: SortOrder };

function rememberedSort(): BotSort {
  try {
    const saved = JSON.parse(localStorage.getItem(SORT_KEY) ?? "null");
    if (columns.some((c) => c.sorter && c.key === saved?.key) && ["ascend", "descend"].includes(saved.order)) return saved;
  } catch {
    /* nothing saved, or unreadable */
  }
  return { key: "", order: null };
}

function rememberSort(sort: BotSort) {
  try {
    localStorage.setItem(SORT_KEY, JSON.stringify(sort));
  } catch {
    /* sorts, just not remembered */
  }
}

/** Recent audit events across all bots (or one broker's). */
function ActivityFeed({ events, bots, title, emptyText }: { events: TradingEvent[]; bots: Bot[]; title: string; emptyText: string }) {
  const symbol = (id: number | null) => {
    const bot = id == null ? undefined : bots.find((b) => b.id === id);
    return id == null ? "system" : bot ? botTitle(bot) : `#${id}`;
  };
  return (
    <Card title={title} size="small" style={{ marginTop: 16 }}>
      <List
        size="small"
        dataSource={events.slice(0, 15)}
        locale={{ emptyText }}
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

/** One real account's money that no bot holds: its cash minus the cash of its bots that aren't archived. Under it,
 * when the bots can't spend all of the cash right now, how much they can and which of Alpaca's figures caps it. */
function FreeCash({ a }: { a: AccountInfo }) {
  const short = a.free != null && a.free < 0;
  const capped = a.cash != null && a.spendable != null && a.spendable < a.cash - 0.005;
  const cap = a.spendable === a.non_marginable_buying_power ? "non-margin buying power" : "buying power";
  return (
    <Card size="small">
      <Statistic
        title={
          <Tooltip title="The account's cash minus the cash of its bots that aren't archived (a paused bot keeps its cash for when it resumes). What's left belongs to no bot: room for a new bot or Add money. Read from the broker at most every 30 seconds.">
            Free cash · {BROKER_LABEL[a.broker] ?? a.label}
          </Tooltip>
        }
        value={usd(a.free)}
        valueStyle={{ color: short ? "#ff4d4f" : undefined }}
      />
      <div style={{ color: "#8b98b5", fontSize: 12 }}>
        {a.cash == null
          ? `Account unknown${a.error ? `: ${a.error}` : ""}`
          : `Cash ${usd(a.cash)} − cash of ${a.bots} bot${a.bots === 1 ? "" : "s"} ${usd(a.bots_cash)}`}
        {short && " · the bots count on more than the account has: their buys stop at what it can pay"}
      </div>
      {capped && (
        <Tooltip title="The bots never buy on margin: they spend at most the smallest of the cash, Alpaca's buying power and its non-margin buying power. After the close the non-margin buying power leaves out the day's sales until they settle (the next trading day), and open orders hold some back.">
          <div style={{ color: "#8b98b5", fontSize: 12 }}>
            {`The bots can spend ${usd(a.spendable)} now (Alpaca's ${cap}): the rest once trades settle${a.open_buys > 0 ? ` and ${a.open_buys} open buy order${a.open_buys === 1 ? "" : "s"} fill` : ""}`}
          </div>
        </Tooltip>
      )}
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
  const [brokerPick, setBrokerPick] = useState(rememberedBroker);
  const [sort, setSort] = useState(rememberedSort);

  const bots = usePolling(() => tradingApi.listBots(showArchived), 15_000, [showArchived]);
  const status = usePolling(tradingApi.status, 30_000);
  const accounts = usePolling(tradingApi.accounts, 30_000);
  const list = bots.data ?? [];
  // The brokers the listed bots use, live first. The filter shows with two or more; a remembered pick with no bots listed
  // (any more), or with the filter hidden, shows them all (the pick holds while the list loads)
  const brokers = [...BROKER_ORDER, ...list.map((b) => b.broker)].filter((n, i, all) => all.indexOf(n) === i && list.some((b) => b.broker === n));
  const broker = !bots.data || (brokers.length > 1 && brokers.includes(brokerPick)) ? brokerPick : "all";
  const brokerLabel = (name: string) => BROKER_LABEL[name] ?? name;
  const brokerOptions = [
    { label: `All (${list.length})`, value: "all" },
    ...brokers.map((n) => ({ label: `${brokerLabel(n)} (${list.filter((b) => b.broker === n).length})`, value: n })),
  ];
  const pickBroker = (v: string) => { setBrokerPick(v); rememberBroker(v); };
  // The Sectors filter offers the badges of the bots listed, the most common first
  const [sectorPick, setSectorPick] = useState<string[] | null>(null);
  const sectorChoices = useMemo(() => {
    const count = new Map<string, number>();
    list.forEach((b) => b.sectors.forEach((s) => count.set(s, (count.get(s) ?? 0) + 1)));
    return [...count].sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0])).map(([s, n]) => ({ text: `${s} (${n})`, value: s }));
  }, [list]);
  const sorted = useMemo(() => columns.map((c) => (
    c.key === "sectors"
      ? { ...c, filters: sectorChoices, filteredValue: sectorPick, onFilter: (v: boolean | React.Key, b: Bot) => b.sectors.includes(String(v)) }
      : c.sorter ? { ...c, sortOrder: c.key === sort.key ? sort.order : null } : c
  )), [sort, sectorChoices, sectorPick]);
  const phone = Grid.useBreakpoint().sm === false; // undefined before the first measure: the desktop control
  const events = usePolling(() => tradingApi.allEvents(broker === "all" ? undefined : broker), 15_000, [broker]);

  // Arriving from the backtest page's "Trade this strategy": open the form pre-filled
  useEffect(() => {
    const state = location.state as { prefill?: BotFormInitial } | null;
    if (!state?.prefill) return;
    setInitial(state.prefill);
    setFormOpen(true);
    navigate(location.pathname, { replace: true, state: null }); // don't reopen on refresh
  }, [location, navigate]);

  const shown = useMemo(() => (broker === "all" ? list : list.filter((b) => b.broker === broker)), [list, broker]);
  const totals = useMemo(() => {
    const running = shown.filter((b) => b.status !== "archived");
    // Everything put in (the start plus Add money), so added money never shows as profit
    const allocated = running.reduce((s, b) => s + b.capital, 0);
    const added = running.reduce((s, b) => s + (b.added_cash ?? 0), 0);
    const equity = running.reduce((s, b) => s + b.equity, 0);
    return { allocated, added, equity, pnl: equity - allocated, active: running.filter((b) => b.status === "active").length, count: running.length };
  }, [shown]);

  const create = async (body: BotCreate | object) => {
    const bot = await tradingApi.createBot(body as BotCreate);
    message.success(`Bot created: ${bot.name}`);
    setFormOpen(false);
    navigate(`/trading/${bot.id}`);
  };

  const confirm = useConfirm();
  const haltAll = () => {
    const active = list.filter((b) => b.status === "active");
    const live = active.filter((b) => b.live).length;
    confirm({
      title: "Pause ALL bots?",
      content: (
        <>
          {`Pauses ${active.length} active bot${active.length === 1 ? "" : "s"} on every account`}
          {live > 0 && <> (<Tag color="red" style={{ marginInline: 0 }}>{live} LIVE — real money</Tag>)</>}
          {": no new decisions or buys until you resume each one. Stop-loss and targets keep protecting open positions."}
        </>
      ),
      okText: "Pause all",
      danger: true,
      onOk: halt,
    });
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
        {/* The title side wraps its own tags; the buttons stay on the right until the row can't fit both */}
        <Col flex="1 1 420px" style={{ minWidth: 0 }}>
          <Typography.Title level={4} style={{ margin: 0 }}>Trading bots</Typography.Title>
          <MarketStatus status={status.data} />
        </Col>
        <Col flex="0 1 auto">
          <Space wrap>
            {/* The kill switch sits in a menu, so a stray click next to New dip bot can't reach it; it still asks first */}
            <Dropdown
              trigger={["click"]}
              menu={{
                items: [{ key: "halt", danger: true, icon: <StopOutlined />, label: "Halt all bots", disabled: !list.some((b) => b.status === "active") }],
                onClick: ({ key }) => { if (key === "halt") haltAll(); },
              }}
            >
              <Button>More <DownOutlined style={{ fontSize: 10 }} /></Button>
            </Dropdown>
            <Tooltip title="Watches a list of symbols, buys the ones that just fell and sells them once they're back (see the Dip buyer page)">
              <Button icon={<FallOutlined />} onClick={() => navigate("/dip", { state: { newDipBot: true } })}>
                New dip bot
              </Button>
            </Tooltip>
            <Tooltip title="Holds the strongest few of a group of symbols, rebalanced at each month's end (see the Momentum Rotation page)">
              <Button icon={<RetweetOutlined />} onClick={() => navigate("/rotation", { state: { newRotationBot: true } })}>
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

      {/* On a phone the four options don't fit a row: a list instead */}
      {brokers.length > 1 && (phone ? (
        <Select value={broker} onChange={pickBroker} options={brokerOptions} style={{ width: "100%", marginBottom: 12 }} />
      ) : (
        <div style={{ marginBottom: 12 }}>
          <Segmented value={broker} onChange={(v) => pickBroker(String(v))} options={brokerOptions} />
        </div>
      ))}

      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Capital allocated" value={usd(totals.allocated, 0)} />
            {totals.added > 0 && <span style={{ color: "#8b98b5", fontSize: 12 }}>incl. {usd(totals.added, 0)} added</span>}
          </Card>
        </Col>
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
        {(accounts.data ?? []).filter((a) => broker === "all" || a.broker === broker)
          .sort((x, y) => BROKER_ORDER.indexOf(x.broker) - BROKER_ORDER.indexOf(y.broker)).map((a) => (
          <Col key={a.broker} xs={24} md={12}><FreeCash a={a} /></Col>
        ))}
      </Row>

      <Card
        size="small"
        style={{ marginTop: 16 }}
        title={broker === "all" ? "Bots" : `Bots · ${brokerLabel(broker)}`}
        extra={<Space><span style={{ color: "#8b98b5" }}>Show archived</span><Switch size="small" checked={showArchived} onChange={setShowArchived} /></Space>}
      >
        <Table<Bot>
          rowKey="id"
          columns={sorted}
          dataSource={shown}
          loading={bots.loading && !bots.data}
          pagination={false}
          size="middle"
          scroll={{ x: 1050 }}
          onChange={(_, filters, s) => {
            setSectorPick((filters.sectors as string[] | null) ?? null);
            const one = Array.isArray(s) ? s[0] : s;
            const next = { key: one?.order ? String(one.columnKey) : "", order: one?.order ?? null };
            setSort(next);
            rememberSort(next);
          }}
          onRow={(b) => ({ onClick: (e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/trading/${b.id}`); }, style: { cursor: "pointer" } })}
          locale={{ emptyText: "No bots yet. Run a backtest you trust, then click “Trade this strategy”, or create one with New bot." }}
        />
      </Card>

      <ActivityFeed
        events={events.data ?? []}
        bots={list}
        title={broker === "all" ? "Recent activity" : `Recent activity · ${brokerLabel(broker)}`}
        emptyText={broker === "all" ? "Nothing yet. Create a bot to get started." : "Nothing yet for these bots."}
      />

      <BotForm
        open={formOpen}
        editing={false}
        initial={initial}
        brokers={status.data?.brokers ?? [{ name: "paper", label: "Paper (built-in simulator)", live: false, available: true, reason: "" }]}
        times={slotTimes(status.data)}
        onCancel={() => setFormOpen(false)}
        onSubmit={create}
      />
    </>
  );
}
