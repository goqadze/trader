import { Card } from "antd";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipProps } from "recharts";
import { strategyShortName, type StrategyId } from "../strategies";
import type { BacktestState } from "../types";

const FOCUS = "#4f8cff";
const OTHER = "#4a5670";
const BENCH = "#8b98b5";
const MUTED = "#8b98b5";

type Point = { date: string } & Record<string, number | string>;

/** Every run's equity as a % return on one date axis (runs progress at different speeds, so merge by date). */
function merge(order: StrategyId[], runs: Partial<Record<StrategyId, BacktestState>>): Point[] {
  const byDate = new Map<string, Point>();
  for (const id of order) {
    const run = runs[id];
    if (!run?.initialCash) continue;
    for (const p of run.chart) {
      const pt = byDate.get(p.date) ?? { date: p.date };
      pt[id] = +((p.strategy / run.initialCash - 1) * 100).toFixed(2);
      pt.buyhold = +((p.buyhold / run.initialCash - 1) * 100).toFixed(2);
      byDate.set(p.date, pt);
    }
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

const signed = (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

/** Hover: every strategy's return that day, best first, the highlighted one in bold. */
function HoverCard({ active, payload, label, focus }: TooltipProps<number, string> & { focus?: StrategyId }) {
  if (!active || !payload?.length) return null;
  const rows = [...payload].filter((p) => typeof p.value === "number").sort((a, b) => (b.value as number) - (a.value as number));
  return (
    <div style={{ background: "#182031", border: "1px solid #2a3550", padding: "8px 10px", fontSize: 12 }}>
      <div style={{ color: "#e6ebf5", marginBottom: 4 }}>{label}</div>
      {rows.map((p) => (
        <div key={p.dataKey as string} style={{ display: "flex", justifyContent: "space-between", gap: 16, fontWeight: p.dataKey === focus ? 600 : 400, color: p.dataKey === focus ? "#e6ebf5" : MUTED }}>
          <span>{p.dataKey === "buyhold" ? "Buy & hold" : strategyShortName(p.dataKey as string)}</span>
          <span>{signed(p.value as number)}</span>
        </div>
      ))}
    </div>
  );
}

function Key({ focus }: { focus?: StrategyId }) {
  const swatch = (color: string, dashed = false, width = 2) => (
    <span style={{ display: "inline-block", width: 18, borderTop: `${width}px ${dashed ? "dashed" : "solid"} ${color}`, verticalAlign: "middle", marginRight: 6 }} />
  );
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 16, justifyContent: "center", fontSize: 12, color: MUTED, marginTop: 4 }}>
      {focus && <span style={{ color: "#e6ebf5" }}>{swatch(FOCUS)}{strategyShortName(focus)}</span>}
      <span>{swatch(OTHER, false, 1)}other strategies</span>
      <span>{swatch(BENCH, true)}Buy &amp; hold</span>
    </div>
  );
}

/**
 * All strategies' return over the window on one chart. Eleven lines can't each get a distinct color, so the
 * selected strategy is blue and the rest are thin gray; hover lists every value, and a click on a table row or
 * tab changes which one stands out.
 */
export default function CompareChart({
  order,
  runs,
  focus,
}: {
  order: StrategyId[];
  runs: Partial<Record<StrategyId, BacktestState>>;
  focus: StrategyId | undefined;
}) {
  const data = merge(order, runs);
  const others = order.filter((id) => id !== focus);
  return (
    <Card title="Return over time: all strategies vs. buy & hold" size="small" style={{ marginTop: 16 }}>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={data}>
          <CartesianGrid stroke="#2a3550" vertical={false} />
          <XAxis dataKey="date" tick={{ fill: MUTED, fontSize: 11 }} minTickGap={40} />
          <YAxis tick={{ fill: MUTED, fontSize: 11 }} width={56} tickFormatter={(v: number) => `${v}%`} domain={["auto", "auto"]} />
          <ReferenceLine y={0} stroke="#3a4660" />
          <Tooltip content={<HoverCard focus={focus} />} />
          {others.map((id) => (
            <Line key={id} dataKey={id} stroke={OTHER} strokeWidth={1} dot={false} isAnimationActive={false} />
          ))}
          <Line dataKey="buyhold" stroke={BENCH} strokeDasharray="5 4" strokeWidth={2} dot={false} isAnimationActive={false} />
          {focus && <Line dataKey={focus} stroke={FOCUS} strokeWidth={2.5} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />}
        </LineChart>
      </ResponsiveContainer>
      <Key focus={focus} />
    </Card>
  );
}
