import { Alert, Card, Col, Descriptions, Progress, Row, Statistic, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import { useMemo } from "react";
import EquityChart from "../components/EquityChart";
import { VerdictAlert } from "../components/VerdictView";
import SymbolChart from "./SymbolChart";
import type { DipEvent, DipResult, DipRun, DipSymbolStats, DipTrade } from "./types";

const MUTED = "#8b98b5";
const GREEN = "#33c088";
const RED = "#ef5b6b";
const signed = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);
const money = (v: number) => v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const color = (v: number | null | undefined) => (v == null || v === 0 ? undefined : v > 0 ? GREEN : RED);

const REASON: Record<string, { color: string; label: string }> = {
  target: { color: "green", label: "back up" },
  "stop-loss": { color: "red", label: "stop-loss" },
  time: { color: "gold", label: "time" },
};

function Stat({ title, value, vs, tip, valueColor }: { title: string; value: string; vs?: string; tip?: string; valueColor?: string }) {
  return (
    <Tooltip title={tip}>
      <Statistic
        title={<span style={{ fontSize: 12 }}>{title}</span>}
        valueRender={() => (
          <span style={{ fontSize: 18, color: valueColor }}>
            {value}
            {vs && <span style={{ fontSize: 12, color: MUTED }}> {vs}</span>}
          </span>
        )}
      />
    </Tooltip>
  );
}

interface RoundTrip {
  key: string;
  symbol: string;
  buy: DipTrade;
  sell: DipTrade;
}

/** The round trips: each sale with the buy it closed. */
function roundTrips(trades: DipTrade[]): RoundTrip[] {
  const open: Record<string, DipTrade> = {};
  const out: RoundTrip[] = [];
  trades.forEach((t, i) => {
    if (t.side === "BUY") open[t.symbol] = t;
    else if (open[t.symbol]) {
      out.push({ key: `${t.symbol}-${i}`, symbol: t.symbol, buy: open[t.symbol], sell: t });
      delete open[t.symbol];
    }
  });
  return out.reverse();
}

const when = (t: DipTrade) => `${t.date}${t.time ? ` ${t.time}` : ""}`;

/** "Is it greedy?" in numbers: what a win and a loss are worth, and how often it must win to break even. */
function RiskReward({ r }: { r: DipResult }) {
  const win = r.avg_win_pct ?? 0;
  const loss = Math.abs(r.avg_loss_pct ?? 0);
  const needed = win + loss > 0 ? (loss / (win + loss)) * 100 : null;
  const exits = r.target_exits ?? 0;
  const stops = r.stop_exits ?? 0;
  const s = r.dip_stats;
  return (
    <Descriptions size="small" column={{ xs: 1, md: 2, xl: 3 }} style={{ marginBottom: 8 }}>
      <Descriptions.Item label="Average win · loss">
        <span style={{ color: GREEN }}>{signed(r.avg_win_pct)}</span>&nbsp;·&nbsp;<span style={{ color: RED }}>{signed(r.avg_loss_pct)}</span>
      </Descriptions.Item>
      <Descriptions.Item label="Wins needed to break even">
        <Tooltip title="With these average sizes, the share of trades that must win just to come out even. Above it = profit.">
          {needed == null ? "—" : `${needed.toFixed(0)}%`} <span style={{ color: MUTED }}>(won {r.win_rate_pct.toFixed(0)}%)</span>
        </Tooltip>
      </Descriptions.Item>
      <Descriptions.Item label="Exits">
        {exits} back up · {stops} stop-loss{r.time_exits ? ` · ${r.time_exits} on time` : ""}
      </Descriptions.Item>
      <Descriptions.Item label={s.rebounds != null && r.trades.some((t) => t.low_t && t.side === "BUY") ? "Dips · turned up · bought" : "Dips seen · bought"}>
        <Tooltip title="Falls into the buy zone during the test, how many then turned up from their low (when waiting for the turn), and how many the rules bought">
          {s.dips}{r.trades.some((t) => t.low_t && t.side === "BUY") ? ` · ${s.rebounds}` : ""} · {s.bought}
        </Tooltip>
      </Descriptions.Item>
      <Descriptions.Item label="Not bought (symbol-days)">
        <Tooltip title="Days a symbol was in the buy zone but wasn't bought, by reason">
          <span style={{ fontSize: 12 }}>
            {s.blacklisted_skips} blacklisted · {s.no_slot} no free slot
            {s.trend_skips ? ` · ${s.trend_skips} downtrend` : ""}
            {s.news_vetoes ? ` · ${s.news_vetoes} bearish news` : ""}
          </span>
        </Tooltip>
      </Descriptions.Item>
      <Descriptions.Item label="Time invested">
        <Tooltip title="Share of days that ended holding something; average days per round trip">
          {(r.exposure_pct ?? 0).toFixed(0)}% of days · {r.avg_hold_days ?? "—"} days a trade
        </Tooltip>
      </Descriptions.Item>
    </Descriptions>
  );
}

/** One window of a dip-buyer backtest: the recommendation, the numbers against holding every symbol, the risk/reward
 *  of its trades, the equity curve, and every round trip. */
export default function DipResultView({ title, run }: { title: string; run?: DipRun }) {
  const trips = useMemo(() => (run?.result ? roundTrips(run.result.trades) : []), [run?.result]);
  if (!run) return null;
  const r = run.result;
  const cfg = run.config;
  return (
    <Card
      size="small"
      style={{ marginBottom: 16 }}
      title={
        <span>
          {title} <span style={{ color: MUTED, fontWeight: 400, fontSize: 12 }}>{cfg.start} → {cfg.end} · checks {cfg.interval}</span>
        </span>
      }
    >
      {run.status === "error" && <Alert type="error" showIcon message={run.error ?? "the run failed"} />}
      {!r && run.status !== "error" && (
        <>
          <Progress percent={run.status === "running" ? 60 : 10} status="active" showInfo={false} />
          <Typography.Text style={{ fontSize: 12, color: MUTED }}>
            {run.status === "pending" ? "Waiting for a free slot…" : "Downloading prices and replaying every check…"}
          </Typography.Text>
        </>
      )}
      {r && (
        <>
          {r.verdict && <VerdictAlert verdict={r.verdict} />}
          {r.missing_symbols.length > 0 && (
            <Alert type="warning" showIcon style={{ marginBottom: 16 }} message={`No prices for ${r.missing_symbols.join(", ")}: left out.`} />
          )}
          {r.blacklisted_at_end.length > 0 && (
            <Alert
              type={r.blacklisted_at_end.length >= r.by_symbol.length ? "error" : "info"}
              showIcon
              style={{ marginBottom: 16 }}
              message={`${r.blacklisted_at_end.length} of ${r.by_symbol.length} symbols blacklisted by the end: ${r.blacklisted_at_end.join(", ")}`}
              description={
                r.blacklisted_at_end.length >= r.by_symbol.length
                  ? "Every symbol hit its stop once and was never bought again, so the strategy stopped trading. A live bot needs you to re-enable symbols; try “Blacklist lasts” to see what that does."
                  : "Each hit its stop-loss once and wasn't bought again."
              }
            />
          )}
          <Row gutter={[16, 8]} style={{ marginBottom: 8 }}>
            <Col xs={12} md={6}>
              <Stat title="Return" value={signed(r.total_return_pct)} vs={`vs ${signed(r.buy_hold_return_pct)} held`} valueColor={color(r.total_return_pct)} />
            </Col>
            <Col xs={12} md={6}>
              <Stat title="Worst drop" value={signed(r.max_drawdown_pct)} vs={`vs ${signed(r.buy_hold_max_drawdown_pct)}`} />
            </Col>
            <Col xs={12} md={6}>
              <Stat title="Sharpe" value={r.sharpe?.toFixed(2) ?? "—"} vs={`vs ${r.buy_hold_sharpe?.toFixed(2) ?? "—"}`} tip="Return per unit of ups and downs (annualized). Higher = smoother." />
            </Col>
            <Col xs={12} md={6}>
              <Stat
                title="Round trips · won"
                value={`${r.num_trades}`}
                vs={`${r.win_rate_pct.toFixed(0)}% won · PF ${r.profit_factor == null ? "∞" : r.profit_factor.toFixed(2)}`}
                tip="PF (profit factor) = money won / money lost. Above 1 makes money."
              />
            </Col>
          </Row>
          <RiskReward r={r} />
          <Typography.Text style={{ fontSize: 12, color: MUTED }}>
            "Held" = the {r.benchmark_symbols.length} symbols bought in equal parts at the first check and kept.
            {r.open_position ? ` Still holding at the end: ${r.open_positions.map((p) => p.symbol).join(", ")} (${money(r.unrealized_pnl ?? 0)} open).` : ""}
          </Typography.Text>
          <EquityChart data={r.equity_curve.map((p) => ({ date: p.date, strategy: Math.round(p.equity), buyhold: Math.round(p.price) }))} />
          <Tabs
            style={{ marginTop: 8 }}
            items={[
              {
                key: "charts",
                label: "Charts",
                children: <SymbolChart run={run} />,
              },
              {
                key: "trips",
                label: `Round trips (${trips.length})`,
                children: (
                  <Table<RoundTrip>
                    size="small"
                    rowKey="key"
                    dataSource={trips}
                    pagination={{ pageSize: 10, size: "small" }}
                    scroll={{ x: 900 }}
                    columns={[
                      { title: "Symbol", dataIndex: "symbol", render: (s: string) => <b>{s}</b>, filters: [...new Set(trips.map((t) => t.symbol))].map((s) => ({ text: s, value: s })), onFilter: (v, t) => t.symbol === v },
                      { title: "Bought", key: "b", render: (_, t) => <span>{when(t.buy)} <span style={{ color: MUTED }}>@ ${t.buy.price.toFixed(2)}</span></span> },
                      {
                        title: <Tooltip title="How far under the price the fall started from it was bought; ↗ = how far up from its low (waiting for the turn)">Fall</Tooltip>,
                        key: "fall", align: "right",
                        render: (_, t) => (
                          <span style={{ color: MUTED }}>
                            {signed(t.buy.drop_pct)}
                            {t.buy.rebound_pct != null && <span style={{ color: "#13c2c2" }}> ↗{t.buy.rebound_pct.toFixed(1)}%</span>}
                          </span>
                        ),
                      },
                      { title: "Sold", key: "s", render: (_, t) => <span>{when(t.sell)} <span style={{ color: MUTED }}>@ ${t.sell.price.toFixed(2)}</span></span> },
                      { title: "Why", key: "why", render: (_, t) => <Tag color={REASON[t.sell.reason ?? ""]?.color}>{REASON[t.sell.reason ?? ""]?.label ?? t.sell.reason}</Tag> },
                      { title: "Days", key: "days", align: "right", render: (_, t) => t.sell.hold_days, sorter: (a, b) => (a.sell.hold_days ?? 0) - (b.sell.hold_days ?? 0) },
                      {
                        title: "P&L", key: "pnl", align: "right",
                        sorter: (a, b) => (a.sell.pnl ?? 0) - (b.sell.pnl ?? 0),
                        render: (_, t) => <span style={{ color: color(t.sell.pnl) }}>{money(t.sell.pnl ?? 0)} <span style={{ fontSize: 12 }}>({signed(t.sell.pnl_pct)})</span></span>,
                      },
                    ]}
                  />
                ),
              },
              {
                key: "symbols",
                label: "By symbol",
                children: (
                  <Table<DipSymbolStats>
                    size="small"
                    rowKey="symbol"
                    dataSource={r.by_symbol}
                    pagination={false}
                    columns={[
                      { title: "Symbol", dataIndex: "symbol", render: (s: string) => <b>{s}</b> },
                      { title: "Round trips", dataIndex: "trades", align: "right", sorter: (a, b) => a.trades - b.trades },
                      { title: "Won", key: "won", align: "right", render: (_, s) => (s.trades ? `${s.wins} (${((s.wins / s.trades) * 100).toFixed(0)}%)` : "—") },
                      { title: "Stops", dataIndex: "stops", align: "right" },
                      { title: "P&L", dataIndex: "pnl", align: "right", sorter: (a, b) => a.pnl - b.pnl, defaultSortOrder: "descend", render: (v: number) => <span style={{ color: color(v) }}>{money(v)}</span> },
                      {
                        title: "At the end", dataIndex: "status",
                        render: (st: string) => <Tag color={st === "blacklisted" ? "red" : st === "held" ? "blue" : "default"}>{st}</Tag>,
                      },
                    ]}
                  />
                ),
              },
              {
                key: "events",
                label: `Blacklist & news (${r.events.length})`,
                children: (
                  <Table<DipEvent>
                    size="small"
                    rowKey={(e) => `${e.date}${e.time}${e.symbol}${e.kind}`}
                    dataSource={[...r.events].reverse()}
                    pagination={{ pageSize: 10, size: "small" }}
                    locale={{ emptyText: "Nothing was blacklisted or blocked by news." }}
                    columns={[
                      { title: "When", key: "when", width: 150, render: (_, e) => `${e.date}${e.time ? ` ${e.time}` : ""}` },
                      { title: "What", dataIndex: "kind", width: 110, render: (k: string) => <Tag color={k === "blacklisted" ? "red" : k === "news" ? "gold" : k === "breaker" ? "volcano" : "green"}>{k}</Tag> },
                      { title: "", dataIndex: "text" },
                    ]}
                  />
                ),
              },
            ]}
          />
        </>
      )}
    </Card>
  );
}
