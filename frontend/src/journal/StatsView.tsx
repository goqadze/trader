// What the journal says: the closed trades' numbers (win rate, profit factor, expectancy, R ...), the running P&L,
// a breakdown by setup / feeling / mistake / weekday ..., and the main takeaways in words.

import { Alert, Card, Col, Empty, Row, Segmented, Space, Spin, Statistic, Table, Tag, Tooltip as AntTooltip } from "antd";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { pnlColor, usd } from "../trading/format";
import { journalApi } from "./api";
import { EmotionTag, GREEN, MUTED, RATING_WORDS, RED, dayLabel, emotionLabel, holdText, mistakeLabel, rText } from "./catalog";
import type { JournalFilters, JournalGroup, JournalStats } from "./types";

const GRID = "#2a3550";
const AXIS = { fill: MUTED, fontSize: 11 };
const TIP = { contentStyle: { background: "#182031", border: `1px solid ${GRID}` }, labelStyle: { color: "#e6ebf5" } };

type Dimension = "by_setup" | "by_emotion" | "by_mistake" | "by_plan" | "by_weekday" | "by_hour" | "by_symbol" | "by_tag" | "by_confidence" | "by_grade" | "by_side" | "by_account";
const DIMENSIONS: { key: Dimension; label: string; hint: string }[] = [
  { key: "by_setup", label: "Setup", hint: "Which setups make you money" },
  { key: "by_emotion", label: "Feeling at entry", hint: "What you felt while planning or entering the trade (its Plan and Entry notes)" },
  { key: "by_mistake", label: "Mistake", hint: "What each mistake cost: a trade with two mistakes counts in both" },
  { key: "by_plan", label: "Plan kept", hint: "Trades where you followed your plan vs broke it (from your reviews)" },
  { key: "by_weekday", label: "Weekday", hint: "By the entry's day (New York)" },
  { key: "by_hour", label: "Hour", hint: "By the entry's hour, New York time" },
  { key: "by_symbol", label: "Symbol", hint: "" },
  { key: "by_tag", label: "Tag", hint: "" },
  { key: "by_confidence", label: "Confidence", hint: "How sure you were before entering, 1-5" },
  { key: "by_grade", label: "Grade", hint: "How well you traded it (your review), F to A" },
  { key: "by_side", label: "Long / short", hint: "" },
  { key: "by_account", label: "Account", hint: "" },
];

/** A group's key in words: an emotion's or a mistake's label, "Followed" for the plan ... */
function keyLabel(dim: Dimension, key: string): string {
  if (dim === "by_emotion") return emotionLabel(key);
  if (dim === "by_mistake") return mistakeLabel(key);
  if (dim === "by_plan") return key === "followed" ? "Followed the plan" : "Broke the plan";
  if (dim === "by_grade") return RATING_WORDS.grade[Number(key) - 1];
  if (dim === "by_confidence") return `${key} · ${RATING_WORDS.confidence[Number(key) - 1]}`;
  return key;
}

/** The main takeaways, in words: only the ones the numbers support. */
export function takeaways(s: JournalStats): string[] {
  const out: string[] = [];
  if (s.trades < 20) out.push(`Only ${s.trades} closed trade${s.trades === 1 ? "" : "s"} so far: patterns start to mean something after about 20-30.`);
  const kept = s.by_plan.find((g) => g.key === "followed");
  const broke = s.by_plan.find((g) => g.key === "broke");
  if (kept && broke)
    out.push(`Following your plan: ${kept.trades} trades, ${kept.win_rate}% wins, ${usd(kept.avg_pnl)} on average. Breaking it: ${broke.trades} trades, ${broke.win_rate}% wins, ${usd(broke.avg_pnl)} on average.`);
  const feelings = s.by_emotion.filter((g) => g.trades >= 2).sort((a, b) => a.avg_pnl - b.avg_pnl);
  if (feelings.length && feelings[0].avg_pnl < 0)
    out.push(`Your worst feeling to trade on: ${emotionLabel(feelings[0].key)} (${feelings[0].trades} trades, ${feelings[0].win_rate}% wins, ${usd(feelings[0].pnl)} in total).`);
  if (feelings.length > 1 && feelings[feelings.length - 1].avg_pnl > 0)
    out.push(`Your best: ${emotionLabel(feelings[feelings.length - 1].key)} (${feelings[feelings.length - 1].trades} trades, ${usd(feelings[feelings.length - 1].pnl)}).`);
  const costliest = [...s.by_mistake].sort((a, b) => a.pnl - b.pnl)[0];
  if (costliest && costliest.pnl < 0) out.push(`Costliest mistake: ${mistakeLabel(costliest.key)}, ${usd(costliest.pnl)} over ${costliest.trades} trade${costliest.trades === 1 ? "" : "s"}.`);
  const setups = s.by_setup.filter((g) => g.trades >= 3);
  if (setups.length > 1) {
    const best = setups[0];
    const worst = setups[setups.length - 1];
    out.push(`Best setup: ${best.key} (${usd(best.pnl)} over ${best.trades}); weakest: ${worst.key} (${usd(worst.pnl)} over ${worst.trades}).`);
  }
  if (s.avg_hold_minutes_win && s.avg_hold_minutes_loss && s.avg_hold_minutes_loss > 1.5 * s.avg_hold_minutes_win)
    out.push(`You hold losers ${(s.avg_hold_minutes_loss / s.avg_hold_minutes_win).toFixed(1)}× longer than winners (${holdText(s.avg_hold_minutes_loss)} vs ${holdText(s.avg_hold_minutes_win)}): cutting losses sooner is the classic fix.`);
  if (s.win_rate != null && s.win_rate >= 50 && s.expectancy != null && s.expectancy < 0)
    out.push(`You win more often than you lose, but the losses are bigger: ${usd(s.avg_win)} average win vs ${usd(s.avg_loss)} average loss.`);
  if (s.payoff_ratio != null && s.win_rate != null && s.win_rate < 50 && s.expectancy != null && s.expectancy > 0)
    out.push(`You lose more often than you win, and still make money: your wins are ${s.payoff_ratio}× your losses.`);
  if (s.r_trades < s.trades && s.trades > 0)
    out.push(`${s.trades - s.r_trades} of ${s.trades} closed trades had no stop-loss written down, so they have no R.`);
  return out;
}

function Tile({ title, value, color, hint, sub }: { title: string; value: string; color?: string; hint?: string; sub?: ReactNode }) {
  return (
    <Card size="small" style={{ height: "100%" }}>
      <Statistic title={hint ? <AntTooltip title={hint}><span style={{ borderBottom: `1px dotted ${MUTED}` }}>{title}</span></AntTooltip> : title}
                 value={value} valueStyle={{ color, fontSize: 20 }} />
      {sub && <div style={{ color: MUTED, fontSize: 12, marginTop: 2 }}>{sub}</div>}
    </Card>
  );
}

function Breakdown({ stats }: { stats: JournalStats }) {
  const [dim, setDim] = useState<Dimension>("by_setup");
  const rows = stats[dim];
  const info = DIMENSIONS.find((d) => d.key === dim)!;
  const chart = rows.slice(0, 15).map((g) => ({ ...g, label: keyLabel(dim, g.key) }));
  return (
    <Card size="small" title="Break it down">
      <div style={{ overflowX: "auto", marginBottom: 8 }}>
        <Segmented size="small" value={dim} onChange={(v) => setDim(v as Dimension)} options={DIMENSIONS.map((d) => ({ value: d.key, label: d.label }))} />
      </div>
      {info.hint && <div style={{ color: MUTED, fontSize: 12, marginBottom: 8 }}>{info.hint}</div>}
      {rows.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={
          dim === "by_emotion" ? "No feelings at entry yet: add Plan or Entry notes to your trades"
          : dim === "by_mistake" ? "No mistakes recorded (in your reviews)" : dim === "by_plan" ? "Review your trades: did you follow the plan?" : "Nothing to group yet"} />
      ) : (
        <Row gutter={[16, 12]}>
          <Col xs={24} lg={11}>
            <ResponsiveContainer width="100%" height={Math.max(120, chart.length * 30 + 40)}>
              <BarChart data={chart} layout="vertical" margin={{ left: 0, right: 16, top: 4, bottom: 4 }} barCategoryGap={4}>
                <CartesianGrid stroke={GRID} horizontal={false} />
                <XAxis type="number" tick={AXIS} tickFormatter={(v) => usd(v, 0)} />
                <YAxis type="category" dataKey="label" tick={AXIS} width={120} interval={0} />
                <ReferenceLine x={0} stroke={MUTED} />
                <Tooltip {...TIP} cursor={{ fill: "rgba(79,140,255,0.08)" }}
                         formatter={(v: number, _n, item) => [`${usd(v)} · ${item.payload.trades} trades · ${item.payload.win_rate}% wins`, "P&L"]} />
                <Bar dataKey="pnl" isAnimationActive={false} radius={4} maxBarSize={18}>
                  {chart.map((g) => <Cell key={g.key} fill={g.pnl >= 0 ? GREEN : RED} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </Col>
          <Col xs={24} lg={13}>
            <Table<JournalGroup>
              size="small"
              rowKey="key"
              dataSource={rows}
              pagination={rows.length > 10 ? { pageSize: 10, size: "small" } : false}
              scroll={{ x: 420 }}
              columns={[
                { title: info.label, key: "key", render: (_, g) => dim === "by_emotion" ? <EmotionTag name={g.key} /> : keyLabel(dim, g.key) },
                { title: "Trades", dataIndex: "trades", align: "right" },
                { title: "Win %", dataIndex: "win_rate", align: "right", render: (v: number) => `${v}%` },
                { title: "P&L", dataIndex: "pnl", align: "right", render: (v: number) => <b style={{ color: pnlColor(v) }}>{usd(v)}</b> },
                { title: "Avg", dataIndex: "avg_pnl", align: "right", render: (v: number) => <span style={{ color: pnlColor(v) }}>{usd(v)}</span> },
                { title: "Avg R", dataIndex: "avg_r", align: "right", render: (v: number | null) => rText(v) },
              ]}
            />
          </Col>
        </Row>
      )}
    </Card>
  );
}

export default function StatsView({ filters, reloadKey }: { filters: JournalFilters; reloadKey: number }) {
  const [stats, setStats] = useState<JournalStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    journalApi.stats(filters).then((s) => { if (live) { setStats(s); setError(null); } }, (e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => { live = false; };
  }, [filters, reloadKey]);

  const lines = useMemo(() => (stats ? takeaways(stats) : []), [stats]);
  if (error) return <Alert type="error" showIcon message={error} />;
  if (!stats) return <Spin style={{ display: "block", margin: 48 }} />;
  if (stats.trades === 0)
    return <Empty description={<>No closed trades {Object.values(filters).some(Boolean) ? "match the filters" : "yet"}. {stats.open ? `${stats.open} open.` : ""}</>} />;

  const s = stats;
  const daily = s.daily.map((d) => ({ ...d, label: dayLabel(d.day) }));
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Row gutter={[12, 12]}>
        <Col xs={12} md={8} xl={4}><Tile title="Net P&L" value={usd(s.net_pnl)} color={pnlColor(s.net_pnl)} sub={`fees ${usd(s.fees)}`} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Win rate" value={s.win_rate != null ? `${s.win_rate}%` : "—"} sub={`${s.wins} won · ${s.losses} lost${s.breakeven ? ` · ${s.breakeven} even` : ""}`} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Profit factor" hint="Money won ÷ money lost. Above 1 makes money; 1.5+ is solid" value={s.profit_factor != null ? s.profit_factor.toFixed(2) : s.losses ? "—" : "no losses"} color={s.profit_factor != null ? (s.profit_factor >= 1 ? GREEN : RED) : undefined} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Expectancy" hint="What a trade makes on average: the net P&L ÷ the trades" value={usd(s.expectancy)} color={pnlColor(s.expectancy)} sub="per trade" /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Avg win / loss" hint="The payoff ratio: average win ÷ average loss" value={s.payoff_ratio != null ? `${s.payoff_ratio}×` : "—"} sub={<><span style={{ color: GREEN }}>{usd(s.avg_win)}</span> / <span style={{ color: RED }}>{usd(s.avg_loss)}</span></>} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Average R" hint="P&L in units of the risk you planned (entry to stop). +1R = won what you risked" value={rText(s.avg_r)} color={pnlColor(s.avg_r)} sub={s.r_trades ? `${s.r_trades} trades with a stop · total ${rText(s.total_r)}` : "set stops to see R"} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Max drawdown" hint="The deepest the running P&L fell from its high" value={usd(s.max_drawdown)} color={s.max_drawdown < 0 ? RED : undefined} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Streaks" value={s.current_streak > 0 ? `${s.current_streak} W now` : s.current_streak < 0 ? `${-s.current_streak} L now` : "—"} sub={`best ${s.max_win_streak} wins · worst ${s.max_loss_streak} losses`} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Time held" hint="Average from entry to exit" value={holdText(s.avg_hold_minutes_win)} sub={`winners · losers ${holdText(s.avg_hold_minutes_loss)}`} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Best trade" value={s.best ? usd(s.best.pnl) : "—"} color={GREEN} sub={s.best && <Link to={`/journal/trades/${s.best.id}`}>{s.best.symbol} · {dayLabel(s.best.day)}</Link>} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Worst trade" value={s.worst ? usd(s.worst.pnl) : "—"} color={RED} sub={s.worst && <Link to={`/journal/trades/${s.worst.id}`}>{s.worst.symbol} · {dayLabel(s.worst.day)}</Link>} /></Col>
        <Col xs={12} md={8} xl={4}><Tile title="Not closed" value={`${s.open} open`} sub={`${s.planned} planned · ${s.skipped} skipped`} /></Col>
      </Row>

      {lines.length > 0 && (
        <Card size="small" title="What your journal says">
          <ul style={{ margin: 0, paddingLeft: 20 }}>{lines.map((l) => <li key={l} style={{ marginBottom: 4 }}>{l}</li>)}</ul>
        </Card>
      )}

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <Card size="small" title="Running P&L" extra={<span style={{ color: MUTED, fontSize: 12 }}>closed trades, by exit day</span>}>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={daily} margin={{ left: 0, right: 12, top: 8, bottom: 0 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="label" tick={AXIS} minTickGap={40} />
                <YAxis tick={AXIS} width={64} tickFormatter={(v) => usd(v, 0)} />
                <ReferenceLine y={0} stroke={MUTED} />
                <Tooltip {...TIP} formatter={(v: number) => [usd(v), "Running P&L"]} />
                <Line type="linear" dataKey="cumulative" stroke="#4f8cff" strokeWidth={2} dot={daily.length < 40 ? { r: 3 } : false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="P&L by day">
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={daily} margin={{ left: 0, right: 12, top: 8, bottom: 0 }} barCategoryGap={2}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="label" tick={AXIS} minTickGap={40} />
                <YAxis tick={AXIS} width={64} tickFormatter={(v) => usd(v, 0)} />
                <ReferenceLine y={0} stroke={MUTED} />
                <Tooltip {...TIP} cursor={{ fill: "rgba(79,140,255,0.08)" }} formatter={(v: number, _n, item) => [`${usd(v)} · ${item.payload.trades} trade(s)`, "P&L"]} />
                <Bar dataKey="pnl" isAnimationActive={false} radius={2} maxBarSize={24}>
                  {daily.map((d) => <Cell key={d.day} fill={d.pnl >= 0 ? GREEN : RED} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </Col>
      </Row>

      <Breakdown stats={s} />

      <Card size="small" title="Feelings you wrote down" extra={<span style={{ color: MUTED, fontSize: 12 }}>every note and check-in in the period</span>}>
        {s.emotions.length === 0 ? <span style={{ color: MUTED }}>None yet</span> : (
          <Space size={[4, 8]} wrap>
            {s.emotions.map((e) => (
              <span key={e.emotion}><EmotionTag name={e.emotion} /><Tag bordered={false} style={{ marginInlineStart: -6 }}>{e.count}×</Tag></span>
            ))}
          </Space>
        )}
      </Card>
    </Space>
  );
}
