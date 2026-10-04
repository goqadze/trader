import { Alert, Card, Descriptions, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { checkAtText } from "../checkAt";
import ActivityLog from "../components/ActivityLog";
import EquityChart from "../components/EquityChart";
import StatTiles from "../components/StatTiles";
import TradeThisButton from "../components/TradeThisButton";
import { VerdictAlert } from "../components/VerdictView";
import { strategyInfo } from "../strategies";
import type { BacktestState, Trade } from "../types";
import { METRICS } from "./metrics";

const MUTED = "#8b98b5";
const pnlColor = (v: number | undefined) => (v == null ? undefined : v > 0 ? "#33c088" : v < 0 ? "#ef5b6b" : undefined);

const tradeColumns: ColumnsType<Trade & { key: number }> = [
  { title: "Date", dataIndex: "date", width: 110 },
  { title: "Side", dataIndex: "side", width: 70, render: (s: Trade["side"]) => <Tag color={s === "BUY" ? "green" : "red"}>{s}</Tag> },
  { title: "Price", dataIndex: "price", width: 90, render: (p: number) => `$${p.toFixed(2)}` },
  { title: "Shares", dataIndex: "shares", width: 80 },
  {
    title: "P&L",
    dataIndex: "pnl",
    width: 110,
    render: (v: number | undefined, t) =>
      v == null ? "" : <span style={{ color: pnlColor(v) }}>{`${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`}{t.pnl_pct != null && ` (${t.pnl_pct > 0 ? "+" : ""}${t.pnl_pct}%)`}</span>,
  },
  {
    title: "Held",
    dataIndex: "hold_days",
    width: 70,
    render: (d: number | undefined, t) => (t.hold_minutes != null ? `${t.hold_minutes} min` : d == null ? "" : `${d} d`),
  },
  { title: "Exit", dataIndex: "reason", render: (r: string | undefined, t) => (r ? `${r}${t.direction === "short" ? " (short)" : ""}` : "") },
];

/** Everything about one strategy's run: its settings, live tiles, every metric, its equity chart, its trades
 *  (including stop-loss and take-profit exits, which aren't decision days) and the decision log. */
export default function StrategyDetail({ run }: { run: BacktestState }) {
  const cfg = run.config;
  const info = cfg && strategyInfo(cfg.strategy);
  const r = run.result;
  const pct = (v: number | undefined) => (v == null ? "default" : `${+(v * 100).toFixed(2)}%`);
  return (
    <>
      {cfg && info && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
          <div style={{ fontSize: 12, color: MUTED, lineHeight: 1.6, flex: "1 1 400px" }}>
            <Typography.Text strong>{info.name}</Typography.Text> · {info.summary}
            <div>
              <b>Buy:</b> {info.buy} · <b>Sell:</b> {info.sell}
            </div>
            {info.intraday ? (
              <div>
                Ran with: 5-minute bars, one setup a day, out by 15:55 · risk {pct(cfg.risk_pct)} a trade ·{" "}
                {cfg.sides === "long" ? "long only" : "long and short"} · slippage {pct(cfg.slippage_pct)} · breaker{" "}
                {cfg.max_drawdown_pct ? pct(cfg.max_drawdown_pct) : "off"}
              </div>
            ) : (
              <div>
                Ran with: every {cfg.rebalance_days} day{cfg.rebalance_days === 1 ? "" : "s"} · check{" "}
                {checkAtText(cfg.decide_at)} · stop {pct(cfg.stop_pct)} · target{" "}
                {pct(cfg.target_pct)} · min confidence {cfg.min_confidence} · breaker{" "}
                {cfg.max_drawdown_pct ? pct(cfg.max_drawdown_pct) : "off"} · news {cfg.news === false ? "off (technical only)" : "on"} ·{" "}
                <a href={`/guides/trading-strategies.html#${info.id}`} target="_blank" rel="noreferrer">
                  chart &amp; details
                </a>
              </div>
            )}
          </div>
          {r && <TradeThisButton config={cfg} type="default" />}
        </div>
      )}
      {run.error && <Alert type="error" showIcon message={run.error} style={{ marginBottom: 16 }} />}
      {r?.verdict && <VerdictAlert verdict={r.verdict} />}
      <StatTiles state={run} />
      {r && (
        <Card title="All measurements" size="small" style={{ marginTop: 16 }}>
          <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3, xxl: 4 }} colon={false}>
            {METRICS.map((m) => {
              const bh = m.buyHold?.(r);
              return (
                <Descriptions.Item
                  key={m.key}
                  label={
                    <Tooltip title={m.help}>
                      <span style={{ borderBottom: `1px dotted ${MUTED}` }}>{m.title}</span>
                    </Tooltip>
                  }
                >
                  <Space size={6}>
                    <span style={m.warn?.(r) ? { color: "#faad14" } : undefined}>{m.format(m.value(r), r)}</span>
                    {m.buyHold && <span style={{ color: MUTED, fontSize: 12 }}>B&amp;H {m.format(bh, r)}</span>}
                  </Space>
                </Descriptions.Item>
              );
            })}
          </Descriptions>
        </Card>
      )}
      <EquityChart data={run.chart} />
      <Card title={`Trades (${r?.trades.length ?? run.trades})`} size="small" style={{ marginTop: 16 }}>
        <Table
          columns={tradeColumns}
          dataSource={(r?.trades ?? []).map((t, i) => ({ ...t, key: i }))}
          size="small"
          pagination={false}
          scroll={{ y: 240 }}
          locale={{ emptyText: r ? "No trades: the strategy's setup didn't appear, or its signals were under the min confidence" : "Waiting for the run to finish" }}
        />
      </Card>
      <ActivityLog rows={run.log} />
    </>
  );
}
