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
import { BotRating } from "./rating";
import { SectorTags } from "./sectors";
import { SetupButton, SetupPct } from "./setup";
import { dipRules } from "../dip/DipParts";
import { strategyName } from "../strategies";
import {
  STATUS_COLOR, botTitle, checkTimes, holdsMany, isDip, isRotation, localTime, nyTime, pct, pnlColor, relative, rotationRules, slotTimes, usd,
} from "./format";
import type { AccountInfo, Bot, BotCreate, BotKind, BotSort, BotTable, BotTableQuery, TradingEvent, TradingStatus } from "./types";
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

// The table is sorted, filtered and cut into pages by trading-service (GET /bots/table): `sorter: true` marks a column
// it sorts by, under the same key (schemas.BotSort)
const columns: ColumnsType<Bot> = [
  {
    // The number reports, emails and Claude call a bot by ("#7"); the same as in its page's address
    title: "ID",
    key: "id",
    width: 70,
    sorter: true,
    sortDirections: ["ascend", "descend"],
    render: (_, b) => <Link to={`/trading/${b.id}`} style={{ color: "#8b98b5", whiteSpace: "nowrap" }}>#{b.id}</Link>,
  },
  {
    // Filterable by kind (the choices and their counts come from trading-service, see below)
    title: "Bot",
    key: "bot",
    render: (_, b) => (
      <Space direction="vertical" size={0}>
        <Link to={`/trading/${b.id}`} style={{ fontWeight: 600, fontSize: 15 }}>{botTitle(b)}</Link>
        <span style={{ color: "#8b98b5", fontSize: 12 }}>{b.name} <SetupButton bot={b} compact /></span>
      </Space>
    ),
  },
  {
    // The owner's favourites, 0-5 stars, set right here (trading/rating.tsx; the stars are drawn in TradingPage, which
    // can reload the list after a click); the first click on the heading puts the most stars on top
    title: "Rating",
    key: "rating",
    width: 130,
    sorter: true,
    sortDirections: ["descend", "ascend"],
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
    sorter: true,
    sortDirections: ["descend", "ascend"],
    render: (_, b) => (
      <Tooltip title={`${isRotation(b) ? `Its ${b.universe?.length} symbols held equally` : isDip(b) ? `Its starting ${b.universe?.length} symbols held equally` : "Buy & hold"} since the bot started: ${pct(b.buy_hold_return_pct)}`}>
        <span style={{ color: pnlColor(b.return_pct) }}>{pct(b.return_pct)}</span>
        <span style={{ color: "#8b98b5", fontSize: 12 }}> / B&amp;H {pct(b.buy_hold_return_pct)}</span>
      </Tooltip>
    ),
  },
  {
    // Return minus B&H, in points: the bots that beat holding what they trade by the most on top (no buy & hold yet:
    // —, and last either way). trading-service sorts by the same two rounded numbers
    title: <Tooltip title="Return minus B&H, in percentage points: how far the bot is ahead of buying and holding what it trades">vs B&amp;H</Tooltip>,
    key: "beat",
    width: 96,
    align: "right",
    sorter: true,
    sortDirections: ["descend", "ascend"],
    render: (_, b) => {
      const v = b.buy_hold_return_pct == null ? null : b.return_pct - b.buy_hold_return_pct;
      return <span style={{ color: pnlColor(v) }}>{pct(v)}</span>;
    },
  },
  {
    title: "Position",
    key: "pos",
    // By the open positions' gain or loss in dollars (flat or all cash: $0)
    sorter: true,
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
  {
    // Its tested setup's numbers (trading/setup.tsx), to sort bots by how their setup did in the backtests; a bot not
    // started from a test sorts last either way
    title: <Tooltip title="Its tested setup's score: the weaker of its practice and exam returns (— = not started from a test)">Score</Tooltip>,
    key: "score",
    width: 84,
    align: "right",
    sorter: true,
    sortDirections: ["descend", "ascend"],
    render: (_, b) => <SetupPct v={b.setup?.score} />,
  },
  {
    title: <Tooltip title="Its tested setup's All: every test window back to back (the bear market left out), what it grew to">All</Tooltip>,
    key: "all",
    width: 76,
    align: "right",
    sorter: true,
    sortDirections: ["descend", "ascend"],
    render: (_, b) => <SetupPct v={b.setup?.all} digits={0} />,
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
// The Bot filter's choices, named as the column names the bots (format.botTitle)
const KIND_LABEL: Record<BotKind, string> = { dip: "Dip buyer", rotation: "Momentum Rotation", symbol: "Single symbol" };

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

// The bot table's sort (ID, Rating, Score, All, Return, vs B&H or Position, by a click on the column) and page size, remembered
// in this browser too
const SORT_KEY = "trading.botSort";
const PAGE_SIZE_KEY = "trading.botPageSize";
const PAGE_SIZES = [25, 50, 100, 200];
type ColumnSort = { key: string; order: SortOrder };

function rememberedSort(): ColumnSort {
  try {
    const saved = JSON.parse(localStorage.getItem(SORT_KEY) ?? "null");
    if (columns.some((c) => c.sorter && c.key === saved?.key) && ["ascend", "descend"].includes(saved.order)) return saved;
  } catch {
    /* nothing saved, or unreadable */
  }
  return { key: "", order: null };
}

function rememberSort(sort: ColumnSort) {
  try {
    localStorage.setItem(SORT_KEY, JSON.stringify(sort));
  } catch {
    /* sorts, just not remembered */
  }
}

function rememberedPageSize(): number {
  try {
    const n = Number(localStorage.getItem(PAGE_SIZE_KEY));
    return PAGE_SIZES.includes(n) ? n : 50;
  } catch {
    return 50;
  }
}

function rememberPageSize(n: number) {
  try {
    localStorage.setItem(PAGE_SIZE_KEY, String(n));
  } catch {
    /* pages, just not remembered */
  }
}

/** Recent audit events across all bots (or one broker's). */
function ActivityFeed({ events, bots, title, emptyText }: { events: TradingEvent[]; bots: BotTable["brief"]; title: string; emptyText: string }) {
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
  const [kindPick, setKindPick] = useState<BotKind[] | null>(null);
  const [sectorPick, setSectorPick] = useState<string[] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(rememberedPageSize);

  // One page of the table, filtered, sorted and cut by trading-service, with the counts and totals of all the bots
  const query: BotTableQuery = {
    broker: brokerPick, archived: showArchived, kinds: kindPick ?? [], sectors: sectorPick ?? [], sort: sort.order ? (sort.key as BotSort) : null,
    order: sort.order === "ascend" ? "asc" : "desc", offset: (page - 1) * pageSize, limit: pageSize,
  };
  const bots = usePolling(() => tradingApi.botTable(query), 15_000, [JSON.stringify(query)]);
  const status = usePolling(tradingApi.status, 30_000);
  const accounts = usePolling(tradingApi.accounts, 30_000);
  const data = bots.data;
  const reloadBots = bots.reload;
  // The brokers the bots use, live first. The filter shows with two or more; a remembered pick with no bots (any more),
  // or with the filter hidden, shows them all, as trading-service does (the pick holds while the page loads)
  const counts = data?.brokers ?? {};
  const brokers = [...BROKER_ORDER, ...Object.keys(counts)].filter((n, i, all) => all.indexOf(n) === i && counts[n]);
  const broker = !data || (brokers.length > 1 && brokers.includes(brokerPick)) ? brokerPick : "all";
  const brokerLabel = (name: string) => BROKER_LABEL[name] ?? name;
  const brokerOptions = [
    { label: `All (${Object.values(counts).reduce((s, n) => s + n, 0)})`, value: "all" },
    ...brokers.map((n) => ({ label: `${brokerLabel(n)} (${counts[n]})`, value: n })),
  ];
  const pickBroker = (v: string) => { setBrokerPick(v); rememberBroker(v); setPage(1); };
  // The Sectors filter offers the badges of the shown broker's bots, the most common first
  const sectorChoices = useMemo(
    () => Object.entries(data?.sectors ?? {}).map(([s, n]) => ({ text: `${s} (${n})`, value: s })), [data?.sectors]);
  // The Bot filter offers the kinds the shown broker has, the most common first
  const kindChoices = useMemo(
    () => (Object.entries(data?.kinds ?? {}) as [BotKind, number][]).map(([k, n]) => ({ text: `${KIND_LABEL[k]} (${n})`, value: k })),
    [data?.kinds]);
  const sorted = useMemo(() => columns.map((c) => (
    c.key === "bot"
      ? { ...c, filters: kindChoices, filteredValue: kindPick }
      : c.key === "sectors"
      ? { ...c, filters: sectorChoices, filteredValue: sectorPick }
      : c.key === "rating"
      ? { ...c, sortOrder: c.key === sort.key ? sort.order : null, render: (_: unknown, b: Bot) => <BotRating bot={b} onChanged={reloadBots} /> }
      : c.sorter ? { ...c, sortOrder: c.key === sort.key ? sort.order : null } : c
  )), [sort, kindChoices, kindPick, sectorChoices, sectorPick, reloadBots]);
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

  // The filters left fewer bots than this page starts at: trading-service answered with the last page
  useEffect(() => {
    if (data && data.offset !== (page - 1) * pageSize) setPage(Math.floor(data.offset / pageSize) + 1);
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  // The shown broker's bots that aren't archived, added up by trading-service: everything put in (the start plus Add
  // money), so added money never shows as profit; money taken out still counts as made, like each bot's return
  const totals = data?.totals ?? { capital: 0, added: 0, withdrawn: 0, equity: 0, pnl: 0, active: 0, count: 0 };

  const create = async (body: BotCreate | object) => {
    const bot = await tradingApi.createBot(body as BotCreate);
    message.success(`Bot created: ${bot.name}`);
    setFormOpen(false);
    navigate(`/trading/${bot.id}`);
  };

  const confirm = useConfirm();
  const haltAll = () => {
    const active = data?.active ?? 0;
    const live = data?.active_live ?? 0;
    confirm({
      title: "Pause ALL bots?",
      content: (
        <>
          {`Pauses ${active} active bot${active === 1 ? "" : "s"} on every account`}
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
                items: [{ key: "halt", danger: true, icon: <StopOutlined />, label: "Halt all bots", disabled: !data?.active }],
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
            <Statistic title="Capital allocated" value={usd(totals.capital, 0)} />
            {(totals.added > 0 || totals.withdrawn > 0) && (
              <span style={{ color: "#8b98b5", fontSize: 12 }}>
                {[totals.added > 0 && `incl. ${usd(totals.added, 0)} added`, totals.withdrawn > 0 && `${usd(totals.withdrawn, 0)} taken out`].filter(Boolean).join(" · ")}
              </span>
            )}
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
        extra={<Space><span style={{ color: "#8b98b5" }}>Show archived</span><Switch size="small" checked={showArchived} onChange={(v) => { setShowArchived(v); setPage(1); }} /></Space>}
      >
        <Table<Bot>
          rowKey="id"
          columns={sorted}
          dataSource={data?.items ?? []}
          loading={bots.loading}
          pagination={{
            current: page, pageSize, total: data?.total ?? 0, showSizeChanger: true, pageSizeOptions: PAGE_SIZES,
            showTotal: (n, [from, to]) => `${from}–${to} of ${n}`, size: phone ? "small" : "default",
          }}
          size="middle"
          scroll={{ x: 1436 }}
          onChange={(p, filters, s, { action }) => {
            if (action === "paginate") {
              if (p.pageSize && p.pageSize !== pageSize) {
                setPageSize(p.pageSize);
                rememberPageSize(p.pageSize);
                setPage(1);
              } else setPage(p.current ?? 1);
              return;
            }
            // A new sort, kind or sector filter starts again from the first page
            setKindPick((filters.bot as BotKind[] | null) ?? null);
            setSectorPick((filters.sectors as string[] | null) ?? null);
            const one = Array.isArray(s) ? s[0] : s;
            const next = { key: one?.order ? String(one.columnKey) : "", order: one?.order ?? null };
            setSort(next);
            rememberSort(next);
            setPage(1);
          }}
          onRow={(b) => ({ onClick: (e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/trading/${b.id}`); }, style: { cursor: "pointer" } })}
          locale={{ emptyText: "No bots yet. Run a backtest you trust, then click “Trade this strategy”, or create one with New bot." }}
        />
      </Card>

      <ActivityFeed
        events={events.data ?? []}
        bots={data?.brief ?? []}
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
