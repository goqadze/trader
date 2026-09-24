import {
  ArrowLeftOutlined, CaretRightOutlined, EditOutlined, InboxOutlined, PauseOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert, App as AntApp, Button, Card, Col, Descriptions, Empty, Popconfirm, Row, Space, Statistic, Table, Tabs, Tag, Tooltip, Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { CartesianGrid, Legend, Line, LineChart, ReferenceDot, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { tradingApi } from "./api";
import BotForm from "./BotForm";
import {
  ACTION_COLOR, ORDER_STATUS_COLOR, SENTIMENT_COLOR, STATUS_COLOR, frac, localTime, pct, pnlColor, usd,
} from "./format";
import { MarketStatus } from "./TradingPage";
import type { Bot, BotUpdate, Decision, Order, Snapshot, TradingEvent } from "./types";
import { usePolling } from "./usePolling";

const MUTED = "#8b98b5";

// ---------------------------------------------------------------------------
// History tables
// ---------------------------------------------------------------------------

const KIND_LABEL: Record<string, { color: string; label: string; tip: string }> = {
  scheduled: { color: "blue", label: "scheduled", tip: "The daily decision made by the scheduler" },
  manual: { color: "purple", label: "manual", tip: "Run now while the market was open (could trade)" },
  preview: { color: "default", label: "preview", tip: "Run now while closed or paused: shows what the bot would do, sent nothing" },
};

const decisionColumns: ColumnsType<Decision> = [
  { title: "When", dataIndex: "created_at", width: 150, render: (v: string) => localTime(v) },
  {
    title: "Type", dataIndex: "kind", width: 100,
    render: (k: string) => <Tooltip title={KIND_LABEL[k]?.tip}><Tag color={KIND_LABEL[k]?.color}>{KIND_LABEL[k]?.label ?? k}</Tag></Tooltip>,
  },
  { title: "Signal", dataIndex: "action", width: 80, render: (a: string) => <Tag color={ACTION_COLOR[a]}>{a}</Tag> },
  { title: "Conf.", dataIndex: "confidence", width: 65, render: (c: number) => c.toFixed(2) },
  { title: "News", dataIndex: "sentiment", width: 95, render: (s: string) => (s ? <Tag color={SENTIMENT_COLOR[s] ?? "default"}>{s}</Tag> : "") },
  { title: "Price", dataIndex: "price", width: 90, align: "right", render: (p: number | null) => usd(p) },
  { title: "What the bot did", dataIndex: "outcome", ellipsis: { showTitle: true } },
];

const orderColumns: ColumnsType<Order> = [
  { title: "When", dataIndex: "created_at", width: 150, render: (v: string) => localTime(v) },
  { title: "Side", dataIndex: "side", width: 70, render: (s: string) => <Tag color={s === "BUY" ? "green" : "red"}>{s}</Tag> },
  {
    title: "Type", dataIndex: "order_type", width: 120,
    render: (t: string, o) => (t === "stop"
      ? <Tooltip title="Stop order held at the broker: becomes a market sell if the price trades at or below this level">stop @ {usd(o.stop_price)}</Tooltip>
      : "market"),
  },
  { title: "Reason", dataIndex: "reason", width: 95, render: (r: string) => <Tag color={r === "stop-loss" ? "red" : r === "target" ? "green" : r === "manual" ? "purple" : "default"}>{r}</Tag> },
  { title: "Qty", key: "qty", width: 80, align: "right", render: (_, o) => (o.filled_qty && o.filled_qty !== o.qty ? `${o.filled_qty}/${o.qty}` : o.qty) },
  { title: "Fill price", dataIndex: "avg_price", width: 100, align: "right", render: (p: number | null) => usd(p) },
  { title: "Fee", dataIndex: "fee", width: 70, align: "right", render: (f: number) => usd(f) },
  { title: "P&L", dataIndex: "pnl", width: 100, align: "right", render: (p: number | null) => <span style={{ color: pnlColor(p) }}>{p == null ? "" : usd(p)}</span> },
  {
    title: "Status", dataIndex: "status", width: 120,
    render: (s: string, o) => (o.order_type === "stop" && (s === "new" || s === "submitted")
      // A stop order stays open for as long as the position is held: "resting", not stuck
      ? <Tooltip title="Waiting at the broker; fills by itself if the price falls to the stop"><Tag color="blue">resting</Tag></Tooltip>
      : <Tooltip title={o.error}><Tag color={ORDER_STATUS_COLOR[s]}>{s.replace("_", " ")}</Tag></Tooltip>),
  },
  { title: "Order id", dataIndex: "client_order_id", ellipsis: true, render: (v: string) => <Typography.Text copyable={{ text: v }} style={{ color: MUTED, fontSize: 12 }}>{v}</Typography.Text> },
];

const LEVEL_COLOR: Record<string, string> = { info: "default", warning: "orange", error: "red" };
const eventColumns: ColumnsType<TradingEvent> = [
  { title: "When", dataIndex: "created_at", width: 150, render: (v: string) => localTime(v) },
  { title: "Type", dataIndex: "kind", width: 100, render: (k: string, e) => <Tag color={LEVEL_COLOR[e.level]}>{k}</Tag> },
  { title: "Message", dataIndex: "message" },
];

/** Expanded decision row: the agent's full explanation and step-by-step trail. */
function DecisionDetail({ d }: { d: Decision }) {
  return (
    <div style={{ padding: "4px 8px" }}>
      <Typography.Paragraph style={{ marginBottom: 8 }}><b>Outcome:</b> {d.outcome}</Typography.Paragraph>
      {d.reasoning && <Typography.Paragraph style={{ marginBottom: 8 }}><b>Reasoning:</b> {d.reasoning}</Typography.Paragraph>}
      {d.steps.length > 0 && (
        <ol style={{ margin: 0, paddingLeft: 20, color: MUTED, fontSize: 12 }}>
          {d.steps.map((s, i) => <li key={i}>{s}</li>)}
        </ol>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Equity chart
// ---------------------------------------------------------------------------

/** Bot equity per trading day vs. simply buying the stock with the same capital on day one. */
function EquityCard({ bot, snapshots, orders }: { bot: Bot; snapshots: Snapshot[]; orders: Order[] }) {
  const data = snapshots.map((s) => ({
    day: s.day,
    bot: Math.round(s.equity * 100) / 100,
    buyhold: bot.benchmark_price ? Math.round((bot.allocated_cash * s.price) / bot.benchmark_price * 100) / 100 : null,
  }));
  // Mark filled trades on the curve (orders are timestamped; snapshots are New York trading days)
  const nyDay = (iso: string) => new Date(iso).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const markers = orders
    .filter((o) => o.filled_qty > 0)
    .map((o) => ({ o, point: data.find((d) => d.day === nyDay(o.created_at)) }))
    .filter((m) => m.point);

  return (
    <Card title="Equity vs. buy & hold" size="small" style={{ marginTop: 16 }}>
      {data.length < 2 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="The chart fills in as the bot runs: one point per trading day, updated every few minutes while the market is open." />
      ) : (
        <ResponsiveContainer width="100%" height={260}>
          <LineChart data={data}>
            <CartesianGrid stroke="#2a3550" />
            <XAxis dataKey="day" tick={{ fill: MUTED, fontSize: 11 }} minTickGap={40} />
            <YAxis tick={{ fill: MUTED, fontSize: 11 }} width={70} domain={["auto", "auto"]} />
            <RTooltip contentStyle={{ background: "#182031", border: "1px solid #2a3550" }} labelStyle={{ color: "#e6ebf5" }} />
            <Legend />
            <Line type="monotone" dataKey="bot" name={bot.name} stroke="#4f8cff" dot={false} strokeWidth={2} isAnimationActive={false} />
            <Line type="monotone" dataKey="buyhold" name="Buy & hold" stroke={MUTED} dot={false} strokeDasharray="5 4" isAnimationActive={false} />
            {markers.map(({ o, point }) => (
              <ReferenceDot key={o.id} x={point!.day} y={point!.bot} r={5} fill={o.side === "BUY" ? "#33c088" : "#ef5b6b"} stroke="none" />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/** One bot (one symbol + one parameter set): live position, controls, and its full trading history. */
export default function BotPage() {
  const id = Number(useParams().id);
  const { message, modal } = AntApp.useApp();
  const [editOpen, setEditOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  // One poll for everything on the page, so all panels show the same moment
  const page = usePolling(
    () => Promise.all([
      tradingApi.getBot(id), tradingApi.decisions(id), tradingApi.orders(id), tradingApi.events(id), tradingApi.equity(id),
    ]),
    15_000,
    [id],
  );
  const status = usePolling(tradingApi.status, 30_000);
  const [bot, decisions = [], orders = [], events = [], snapshots = []] = page.data ?? [];

  const stats = useMemo(() => {
    const closed = orders.filter((o) => o.side === "SELL" && o.pnl != null);
    const wins = closed.filter((o) => (o.pnl ?? 0) > 0).length;
    return { trades: closed.length, winRate: closed.length ? (wins / closed.length) * 100 : null };
  }, [orders]);

  // Why is it paused? Show the most recent event that paused it (user, breaker, reconcile, halt)
  const pauseReason = useMemo(
    () => events.find((e) => e.kind === "paused" || e.kind === "risk" || e.kind === "reconcile"),
    [events],
  );

  if (page.error && !bot) {
    return <Alert type="error" showIcon message="Couldn't load this bot" description={page.error} action={<Link to="/trading">Back</Link>} />;
  }
  if (!bot) return <Card loading />;

  const marketOpen = status.data?.market_open ?? false;

  /** Run an action, show the server's message on failure, refresh the page either way. */
  const act = async (name: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(name);
    try {
      await fn();
      if (ok) message.success(ok);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      page.reload();
    }
  };

  const runNow = () =>
    act("run", async () => {
      const d = await tradingApi.runNow(bot.id);
      modal.info({
        title: `${d.kind === "preview" ? "Preview" : "Decision"}: ${d.action} (confidence ${d.confidence.toFixed(2)})`,
        content: <DecisionDetail d={d} />,
        width: 640,
      });
    });

  const save = async (body: BotUpdate | object) => {
    await tradingApi.updateBot(bot.id, body as BotUpdate);
    message.success("Parameters saved");
    setEditOpen(false);
    page.reload();
  };

  const distance = (level: number | null) =>
    level != null && bot.last_price ? pct(((level - bot.last_price) / bot.last_price) * 100) : "—";

  return (
    <>
      <Link to="/trading" style={{ color: MUTED }}><ArrowLeftOutlined /> All bots</Link>

      {/* ---------- Header: identity + controls ---------- */}
      <Row justify="space-between" align="middle" gutter={[16, 12]} style={{ margin: "8px 0 16px" }}>
        <Col>
          <Space align="center" wrap>
            <Typography.Title level={3} style={{ margin: 0 }}>{bot.symbol}</Typography.Title>
            <span style={{ color: MUTED }}>{bot.name}</span>
            <Tag color={STATUS_COLOR[bot.status]}>{bot.status}</Tag>
            <Tag color={bot.live ? "red" : "blue"}>{bot.live ? "LIVE — real money" : bot.broker === "paper" ? "PAPER (simulated)" : bot.broker}</Tag>
            {bot.pending_order && <Tag color="processing">order pending</Tag>}
          </Space>
          <div style={{ marginTop: 4 }}><MarketStatus status={status.data} /></div>
        </Col>
        {bot.status !== "archived" && (
          <Col>
            <Space wrap>
              <Tooltip title={marketOpen && bot.status === "active" ? "Decide now and trade on the result" : "Market closed or bot paused: shows what the bot would do, sends no order"}>
                <Button icon={<ThunderboltOutlined />} loading={busy === "run"} onClick={runNow}>
                  {marketOpen && bot.status === "active" ? "Run now" : "Preview decision"}
                </Button>
              </Tooltip>
              {bot.status === "active" ? (
                <Button icon={<PauseOutlined />} loading={busy === "pause"} onClick={() => act("pause", () => tradingApi.pause(bot.id), "Paused")}>Pause</Button>
              ) : (
                <Button type="primary" icon={<CaretRightOutlined />} loading={busy === "resume"} onClick={() => act("resume", () => tradingApi.resume(bot.id), "Resumed")}>Resume</Button>
              )}
              <Popconfirm
                title={`Sell all ${bot.shares} ${bot.symbol} at market now?`}
                okText="Sell now"
                okButtonProps={{ danger: true }}
                onConfirm={() => act("close", () => tradingApi.closePosition(bot.id), "Sell order sent")}
                disabled={bot.shares === 0 || !marketOpen || bot.pending_order}
              >
                <Tooltip title={bot.shares === 0 ? "No open position" : !marketOpen ? "Orders only during market hours" : ""}>
                  <Button danger loading={busy === "close"} disabled={bot.shares === 0 || !marketOpen || bot.pending_order}>Close position</Button>
                </Tooltip>
              </Popconfirm>
              <Button icon={<EditOutlined />} onClick={() => setEditOpen(true)}>Edit</Button>
              <Popconfirm
                title="Archive this bot?"
                description="It stops for good and is hidden from the list. Its history is kept."
                onConfirm={() => act("archive", () => tradingApi.archive(bot.id), "Archived")}
                disabled={bot.shares > 0 || bot.pending_order}
              >
                <Tooltip title={bot.shares > 0 ? "Close the position first" : ""}>
                  <Button icon={<InboxOutlined />} disabled={bot.shares > 0 || bot.pending_order}>Archive</Button>
                </Tooltip>
              </Popconfirm>
            </Space>
          </Col>
        )}
      </Row>

      {bot.status === "paused" && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="Paused: no new decisions. Stop-loss and take-profit still protect any open position."
          description={pauseReason ? `${localTime(pauseReason.created_at)}: ${pauseReason.message}` : undefined} />
      )}
      {page.error && <Alert type="error" showIcon message={`Refresh failed: ${page.error}`} style={{ marginBottom: 16 }} />}

      {/* ---------- KPIs ---------- */}
      <Row gutter={[16, 16]}>
        <Col xs={12} md={8} xl={4}><Card size="small"><Statistic title="Equity" value={usd(bot.equity)} /><span style={{ color: MUTED, fontSize: 12 }}>of {usd(bot.allocated_cash, 0)} allocated</span></Card></Col>
        <Col xs={12} md={8} xl={4}>
          <Card size="small">
            <Statistic title="Return" value={pct(bot.return_pct)} valueStyle={{ color: pnlColor(bot.return_pct) }} />
            <span style={{ color: MUTED, fontSize: 12 }}>buy &amp; hold {pct(bot.buy_hold_return_pct)}</span>
          </Card>
        </Col>
        <Col xs={12} md={8} xl={4}><Card size="small"><Statistic title="Realized P&L" value={usd(bot.realized_pnl)} valueStyle={{ color: pnlColor(bot.realized_pnl) }} /><span style={{ color: MUTED, fontSize: 12 }}>closed trades</span></Card></Col>
        <Col xs={12} md={8} xl={4}><Card size="small"><Statistic title="Unrealized P&L" value={usd(bot.unrealized_pnl)} valueStyle={{ color: pnlColor(bot.unrealized_pnl) }} /><span style={{ color: MUTED, fontSize: 12 }}>open position</span></Card></Col>
        <Col xs={12} md={8} xl={4}><Card size="small"><Statistic title="Cash" value={usd(bot.cash)} /><span style={{ color: MUTED, fontSize: 12 }}>uninvested</span></Card></Col>
        <Col xs={12} md={8} xl={4}>
          <Card size="small">
            <Statistic title="Win rate" value={stats.winRate == null ? "—" : `${stats.winRate.toFixed(0)}%`} />
            <span style={{ color: MUTED, fontSize: 12 }}>{stats.trades} closed trade{stats.trades === 1 ? "" : "s"}</span>
          </Card>
        </Col>
      </Row>

      {/* ---------- Position + parameters ---------- */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={12}>
          <Card title="Position" size="small" style={{ height: "100%" }}>
            {bot.shares > 0 ? (
              <Descriptions column={2} size="small">
                <Descriptions.Item label="Shares">{bot.shares}</Descriptions.Item>
                <Descriptions.Item label="Entry">{usd(bot.entry_price)}</Descriptions.Item>
                <Descriptions.Item label="Last price">{usd(bot.last_price)}</Descriptions.Item>
                <Descriptions.Item label="Cost basis">{usd(bot.cost_basis)}</Descriptions.Item>
                <Descriptions.Item label="Stop-loss">
                  <Space size={6} wrap>
                    <span style={{ whiteSpace: "nowrap" }}>
                      <span style={{ color: "#ef5b6b" }}>{usd(bot.stop_price)}</span>&nbsp;<span style={{ color: MUTED }}>({distance(bot.stop_price)})</span>
                    </span>
                    {bot.stop_at_broker ? (
                      <Tooltip title="A real stop order waits at the broker, so it fires even while this app or your computer is off">
                        <Tag color="green" style={{ marginInlineEnd: 0 }}>held at broker</Tag>
                      </Tooltip>
                    ) : (
                      <Tooltip title={`Checked by this app every ${status.data?.risk_check_minutes ?? 5} min during market hours, so only while it is running`}>
                        <Tag style={{ marginInlineEnd: 0 }}>checked by app</Tag>
                      </Tooltip>
                    )}
                  </Space>
                </Descriptions.Item>
                <Descriptions.Item label="Take-profit"><span style={{ color: "#33c088" }}>{usd(bot.target_price)}</span>&nbsp;<span style={{ color: MUTED }}>({distance(bot.target_price)})</span></Descriptions.Item>
              </Descriptions>
            ) : (
              <Space direction="vertical" size={2}>
                <span>Flat, holding cash.</span>
                <span style={{ color: MUTED }}>Last price {usd(bot.last_price)} · updated {localTime(bot.last_price_at)}</span>
              </Space>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="Parameters" size="small" style={{ height: "100%" }} extra={<Button size="small" type="link" onClick={() => setEditOpen(true)}>Edit</Button>}>
            <Descriptions column={3} size="small">
              <Descriptions.Item label="Mode">{bot.mode}</Descriptions.Item>
              <Descriptions.Item label="Min conf.">{bot.min_confidence}</Descriptions.Item>
              <Descriptions.Item label="Every">{bot.rebalance_days} trading day{bot.rebalance_days === 1 ? "" : "s"}</Descriptions.Item>
              <Descriptions.Item label="Position">{frac(bot.position_pct)} of cash</Descriptions.Item>
              <Descriptions.Item label="Stop">{frac(bot.stop_pct)}</Descriptions.Item>
              <Descriptions.Item label="Target">{frac(bot.target_pct)}</Descriptions.Item>
              <Descriptions.Item label="Breaker">{bot.max_drawdown_pct ? `${frac(bot.max_drawdown_pct)} drawdown` : "off"}</Descriptions.Item>
              <Descriptions.Item label="Slippage">{frac(bot.slippage_pct)}</Descriptions.Item>
              <Descriptions.Item label="Fee">{frac(bot.fee_pct)}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <EquityCard bot={bot} snapshots={snapshots} orders={orders} />

      {/* ---------- History ---------- */}
      <Card size="small" style={{ marginTop: 16 }}>
        <Tabs
          items={[
            {
              key: "decisions",
              label: `Decisions (${decisions.length})`,
              children: (
                <Table<Decision>
                  rowKey="id" size="small" columns={decisionColumns} dataSource={decisions}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }} scroll={{ x: 800 }}
                  expandable={{ expandedRowRender: (d) => <DecisionDetail d={d} /> }}
                  locale={{ emptyText: "No decisions yet. The first one happens at the next decision time, or use Preview decision." }}
                />
              ),
            },
            {
              key: "orders",
              label: `Orders (${orders.length})`,
              children: (
                <Table<Order>
                  rowKey="id" size="small" columns={orderColumns} dataSource={orders}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }} scroll={{ x: 900 }}
                  locale={{ emptyText: "No orders yet." }}
                />
              ),
            },
            {
              key: "events",
              label: `Audit log (${events.length})`,
              children: (
                <Table<TradingEvent>
                  rowKey="id" size="small" columns={eventColumns} dataSource={events}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }}
                />
              ),
            },
          ]}
        />
      </Card>

      <BotForm
        open={editOpen}
        editing
        initial={bot}
        brokers={status.data?.brokers ?? []}
        onCancel={() => setEditOpen(false)}
        onSubmit={save}
      />
    </>
  );
}
