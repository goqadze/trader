import { ZoomInOutlined, ZoomOutOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Space, Spin, Table, Tag, Tooltip, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid, Line, LineChart, ReferenceArea, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis,
} from "recharts";
import { getRunPrices } from "../api";
import type { DipRun, DipTrade } from "./types";

const MUTED = "#8b98b5";
const RED = "#ef5b6b";
const GREEN = "#33c088";
const ORANGE = "#e8a23b";
const CYAN = "#13c2c2";
const DAY = 86_400_000;

/** One trade as the chart draws it: where the fall started, its low (the turn), the buy and (unless still held) the sale. */
export interface ChartTrip {
  key: string;
  peak: { t: number; price: number };
  low?: { t: number; price: number };
  buy: { t: number; price: number };
  sell?: { t: number; price: number; reason?: string; pnl_pct?: number };
  target: number;
  stop: number;
  reference?: number;
  turn_pct?: number; // waited for the turn: bought this far up from the low
}

const ms = (iso: string) => Date.parse(iso);

/** The round trips of one symbol (and its open position), from the run's trades. */
export function chartTrips(run: DipRun, symbol: string): ChartTrip[] {
  const r = run.result!;
  const out: ChartTrip[] = [];
  let buy: DipTrade | null = null;
  r.trades.forEach((t, i) => {
    if (t.symbol !== symbol || !t.t || !t.peak_t || t.peak_price == null) return;
    if (t.side === "BUY") {
      buy = t;
      return;
    }
    const b: DipTrade | null = buy;
    // The low the chart shades to: the one it turned up from (waiting for the turn), else the lowest close up to the sale
    const low = b?.low_t && b.low_price != null ? { t: ms(b.low_t), price: b.low_price } : t.low_t && t.low_price != null ? { t: ms(t.low_t), price: t.low_price } : undefined;
    out.push({
      key: `${symbol}-${i}`,
      peak: { t: ms(t.peak_t), price: t.peak_price },
      low,
      buy: { t: ms(t.buy_t ?? b?.t ?? t.t), price: b?.price ?? t.entry ?? t.price },
      sell: { t: ms(t.t), price: t.price, reason: t.reason, pnl_pct: t.pnl_pct },
      target: t.target ?? 0,
      stop: t.stop ?? 0,
      reference: b?.reference,
      turn_pct: b?.rebound_pct,
    });
    buy = null;
  });
  const open = r.open_positions.find((p) => p.symbol === symbol);
  if (open?.buy_t) {
    const b = buy as DipTrade | null;
    out.push({
      key: `${symbol}-open`,
      peak: { t: ms(open.peak_t), price: open.peak_price },
      low: b?.low_t && b.low_price != null ? { t: ms(b.low_t), price: b.low_price } : undefined,
      buy: { t: ms(open.buy_t), price: open.entry },
      target: open.target,
      stop: open.stop,
      reference: b?.reference,
      turn_pct: b?.rebound_pct,
    });
  }
  return out;
}

const REASON: Record<string, string> = { target: "back up", "stop-loss": "stop-loss", time: "time" };

/** Price of one symbol over the backtest with every trade on it: where the fall started (orange), its low where it
 *  turned (cyan), the buy (green) and the sale (green or red by its result), the bearish stretch shaded red and the
 *  bullish one green. Zoom into a trade to see each moment and its price, with the target and stop. */
export default function SymbolChart({ run }: { run: DipRun }) {
  const r = run.result!;
  const intraday = run.config.interval !== "1d";
  const symbols = useMemo(
    () => [...r.by_symbol].sort((a, b) => b.trades - a.trades || a.symbol.localeCompare(b.symbol)).map((s) => s.symbol),
    [r.by_symbol]
  );
  const [symbol, setSymbol] = useState(symbols[0]);
  const [zoom, setZoom] = useState<string | null>(null);
  const [points, setPoints] = useState<{ x: number; p: number }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trips = useMemo(() => chartTrips(run, symbol), [run, symbol]);
  const zoomed = trips.find((t) => t.key === zoom);

  // The stretch to draw: the whole test, or one trade from a little before its fall to a little after its sale
  const range = useMemo(() => {
    if (!zoomed) return null;
    const end = zoomed.sell?.t ?? Date.parse(`${run.config.end}T23:59:59Z`);
    const pad = Math.max((end - zoomed.peak.t) * 0.25, intraday ? DAY / 2 : 5 * DAY);
    return [zoomed.peak.t - pad, end + pad] as const;
  }, [zoomed, run.config.end, intraday]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    const iso = (t: number) => new Date(t).toISOString();
    getRunPrices(run.run_id, symbol, range ? iso(range[0]) : undefined, range ? iso(range[1]) : undefined, range ? 1500 : 900)
      .then((d) => alive && setPoints(d.points.map((pt) => ({ x: ms(pt.t), p: pt.p }))))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [run.run_id, symbol, range]);

  const fmt = (t: number, withTime = intraday) =>
    new Date(t).toLocaleString("en-US", withTime
      ? { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { timeZone: "UTC", year: "2-digit", month: "short", day: "numeric" });

  const lo = points.length ? points[0].x : 0;
  const hi = points.length ? points[points.length - 1].x : 0;
  const shown = trips.filter((t) => (t.sell?.t ?? hi) >= lo && t.peak.t <= hi);
  // The x-axis counts checks, not clock time: nights and weekends take no room (like a trading chart). A moment maps to
  // the first check at or after it (exact when zoomed in, where every check is drawn).
  const at = (t: number) => {
    let a = 0, b = points.length - 1;
    while (a < b) {
      const m = (a + b) >> 1;
      if (points[m].x < t) a = m + 1;
      else b = m;
    }
    return a;
  };
  const data = points.map((pt, i) => ({ i, p: pt.p }));
  const inView = (t: number) => t >= lo && t <= hi; // a moment before the drawn prices (a fall that started earlier) gets no dot
  const timeOf = (i: number) => points[Math.max(0, Math.min(points.length - 1, Math.round(i)))]?.x ?? 0;
  const label = (text: string, position: "top" | "bottom") => (zoomed ? { value: text, position, fill: "#e6ebf5", fontSize: 11 } : undefined);

  return (
    <div>
      {/* One button per symbol, wrapping onto more lines: a long watchlist mustn't widen the page */}
      <Space size={[4, 4]} wrap style={{ marginBottom: 8 }}>
        {symbols.map((s) => {
          const n = r.by_symbol.find((b) => b.symbol === s)?.trades ?? 0;
          return (
            <Button
              key={s}
              size="small"
              type={s === symbol ? "primary" : "default"}
              aria-pressed={s === symbol}
              onClick={() => {
                setSymbol(s);
                setZoom(null);
              }}
            >
              {n ? `${s} (${n})` : s}
            </Button>
          );
        })}
        {zoomed && <Button size="small" icon={<ZoomOutOutlined />} onClick={() => setZoom(null)}>Whole test</Button>}
      </Space>
      <div style={{ fontSize: 12, color: MUTED, marginBottom: 4 }}>
        <span style={{ color: ORANGE }}>●</span> the fall starts (its high) &nbsp;
        <span style={{ background: "rgba(239,91,107,0.25)", padding: "0 6px" }}>bearish</span> &nbsp;
        <span style={{ color: CYAN }}>●</span> its low, where it turned &nbsp;
        <span style={{ background: "rgba(51,192,136,0.25)", padding: "0 6px" }}>bullish</span> &nbsp;
        <span style={{ color: GREEN }}>▲</span> buy &nbsp;
        <span style={{ color: RED }}>▼</span> sell. {zoomed ? "Dashed: the target (green) and the stop (red)." : "Click a trade below to zoom in."}
      </div>
      {error && <Alert type="warning" showIcon message={`Prices unavailable: ${error}`} description="A run's prices live in memory: after backtest-service restarts, run the backtest again." />}
      <Spin spinning={loading}>
        {data.length < 2 && !loading ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No prices for this symbol in the window" />
        ) : (
          <ResponsiveContainer width="100%" height={340}>
            <LineChart data={data} margin={{ top: 18, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="#2a3550" />
              <XAxis dataKey="i" type="number" domain={[0, Math.max(1, data.length - 1)]} tickFormatter={(i) => fmt(timeOf(i))} tick={{ fill: MUTED, fontSize: 11 }} minTickGap={60} />
              <YAxis dataKey="p" domain={["auto", "auto"]} tick={{ fill: MUTED, fontSize: 11 }} width={70} tickFormatter={(v: number) => `$${v.toFixed(v < 10 ? 2 : 0)}`} />
              <RTooltip
                contentStyle={{ background: "#182031", border: "1px solid #2a3550" }}
                labelStyle={{ color: "#e6ebf5" }}
                labelFormatter={(i) => fmt(timeOf(Number(i)), intraday)}
                formatter={(v: number) => [`$${v.toFixed(2)}`, symbol]}
              />
              {shown.map((t) => (
                <ReferenceArea key={`${t.key}-bear`} x1={at(t.peak.t)} x2={at(t.low?.t ?? t.buy.t)} fill={RED} fillOpacity={0.14} ifOverflow="hidden" />
              ))}
              {shown.map((t) => (
                <ReferenceArea key={`${t.key}-bull`} x1={at(t.low?.t ?? t.buy.t)} x2={at(t.sell?.t ?? hi)} fill={GREEN} fillOpacity={0.12} ifOverflow="hidden" />
              ))}
              {zoomed && <ReferenceLine y={zoomed.target} stroke={GREEN} strokeDasharray="5 4" ifOverflow="extendDomain" label={{ value: `target $${zoomed.target.toFixed(2)}`, fill: GREEN, fontSize: 11, position: "insideTopRight" }} />}
              {zoomed && <ReferenceLine y={zoomed.stop} stroke={RED} strokeDasharray="5 4" ifOverflow="extendDomain" label={{ value: `stop $${zoomed.stop.toFixed(2)}`, fill: RED, fontSize: 11, position: "insideBottomRight" }} />}
              <Line type="linear" dataKey="p" stroke="#4f8cff" dot={false} strokeWidth={1.6} isAnimationActive={false} />
              {shown.filter((t) => inView(t.peak.t)).map((t) => (
                <ReferenceDot key={`${t.key}-peak`} x={at(t.peak.t)} y={t.peak.price} r={zoomed ? 6 : 4} fill={ORANGE} stroke="none" ifOverflow="extendDomain"
                  label={label(`high $${t.peak.price.toFixed(2)}`, "top")} />
              ))}
              {shown.filter((t) => t.low && inView(t.low.t)).map((t) => (
                <ReferenceDot key={`${t.key}-low`} x={at(t.low!.t)} y={t.low!.price} r={zoomed ? 6 : 4} fill={CYAN} stroke="none" ifOverflow="extendDomain"
                  label={label(`low $${t.low!.price.toFixed(2)}`, "bottom")} />
              ))}
              {shown.filter((t) => inView(t.buy.t)).map((t) => (
                <ReferenceDot key={`${t.key}-buy`} x={at(t.buy.t)} y={t.buy.price} r={zoomed ? 7 : 5} fill={GREEN} stroke="#0b1220" ifOverflow="extendDomain"
                  label={label(`BUY $${t.buy.price.toFixed(2)}`, "bottom")} />
              ))}
              {shown.filter((t) => t.sell && inView(t.sell.t)).map((t) => (
                <ReferenceDot key={`${t.key}-sell`} x={at(t.sell!.t)} y={t.sell!.price} r={zoomed ? 7 : 5} fill={(t.sell!.pnl_pct ?? 0) >= 0 ? GREEN : RED} stroke="#fff"
                  ifOverflow="extendDomain" label={label(`SELL $${t.sell!.price.toFixed(2)}`, "top")} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </Spin>
      <Table<ChartTrip>
        size="small"
        rowKey="key"
        style={{ marginTop: 8 }}
        dataSource={[...trips].reverse()}
        pagination={{ pageSize: 6, size: "small" }}
        scroll={{ x: 900 }}
        locale={{ emptyText: `${symbol} was never bought in this test.` }}
        rowClassName={(t) => (t.key === zoom ? "ant-table-row-selected" : "")}
        onRow={(t) => ({ onClick: () => setZoom(t.key === zoom ? null : t.key), style: { cursor: "pointer" } })}
        columns={[
          { title: "", key: "z", width: 36, render: (_, t) => <Tooltip title="Zoom in"><ZoomInOutlined style={{ color: t.key === zoom ? "#4f8cff" : MUTED }} /></Tooltip> },
          { title: <span style={{ color: ORANGE }}>Fall starts</span>, key: "peak", render: (_, t) => <span>{fmt(t.peak.t)} <span style={{ color: MUTED }}>${t.peak.price.toFixed(2)}</span></span> },
          { title: <span style={{ color: CYAN }}>Low (turn)</span>, key: "low", render: (_, t) => (t.low ? <span>{fmt(t.low.t)} <span style={{ color: MUTED }}>${t.low.price.toFixed(2)}</span></span> : "—") },
          {
            title: <span style={{ color: GREEN }}>Buy</span>, key: "buy",
            render: (_, t) => (
              <span>
                {fmt(t.buy.t)} <span style={{ color: MUTED }}>${t.buy.price.toFixed(2)}</span>
                {t.turn_pct != null && <Tag color="cyan" style={{ marginLeft: 6 }}>+{t.turn_pct.toFixed(1)}% off the low</Tag>}
              </span>
            ),
          },
          {
            title: "Sell", key: "sell",
            render: (_, t) => (t.sell
              ? <span>{fmt(t.sell.t)} <span style={{ color: MUTED }}>${t.sell.price.toFixed(2)}</span> <Tag color={t.sell.reason === "stop-loss" ? "red" : t.sell.reason === "time" ? "gold" : "green"}>{REASON[t.sell.reason ?? ""] ?? t.sell.reason}</Tag></span>
              : <Tag color="blue">still held</Tag>),
          },
          {
            title: "Result", key: "res", align: "right",
            render: (_, t) => (t.sell?.pnl_pct != null ? <span style={{ color: t.sell.pnl_pct >= 0 ? GREEN : RED }}>{t.sell.pnl_pct > 0 ? "+" : ""}{t.sell.pnl_pct.toFixed(2)}%</span> : "—"),
          },
          {
            title: <Tooltip title="From the fall's start to the sale">Duration</Tooltip>, key: "dur", align: "right",
            render: (_, t) => {
              const d = ((t.sell?.t ?? hi) - t.peak.t) / DAY;
              return d < 1 ? `${Math.round(d * 24)} h` : `${d.toFixed(d < 10 ? 1 : 0)} d`;
            },
          },
        ]}
      />
      <Typography.Text style={{ fontSize: 12, color: MUTED }}>
        Times are New York time{intraday ? "; each point is a check's price (thinned on the whole-test view, every check when zoomed in)" : "; one point per daily close"}.
      </Typography.Text>
    </div>
  );
}
