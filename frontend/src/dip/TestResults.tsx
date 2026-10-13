// The dip buyer's test batches (parameter sweeps): every tested setup under its name, its rules and how it did in each
// window, as a sortable grid. Any of them can be saved as a setup or loaded into the backtest form.
// trading-service keeps them (/dip/studies, /dip/tests); a sweep script fills them in.

import { ExperimentOutlined, SaveOutlined, StarFilled } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, Empty, Grid, Input, Modal, Row, Select, Space, Spin, Switch, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnGroupType, ColumnsType, ColumnType, SortOrder } from "antd/es/table/interface";
import dayjs from "dayjs";
import { useCallback, useEffect, useRef, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { tradingApi } from "../trading/api";
import {
  type DipPresetConfig, type DipStudy, type DipStudySummary, type DipTest, type DipTestDetail, type DipTestNumbers, type DipTestQuery, fallText,
  pickRules, sellText,
} from "./types";

const MUTED = "#8b98b5";
const GREEN = "#33c088";
const RED = "#ef5b6b";
const color = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? undefined : v > 0 ? GREEN : RED);
const signed = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);
const pctOf = (f: number) => `${+(f * 100).toFixed(2)}%`;

const PERIOD_LABEL: Record<string, string> = {
  practice: "Practice", exam: "Exam", stress: "Stress check", early: "Early check", practice4: "Practice", exam4: "Exam",
  bear: "Bear 2022",
};
const months = (p?: [string, string]) => (p ? Math.round(dayjs(p[1]).diff(dayjs(p[0]), "day") / 30.44) : 0);
const PERIOD_TIP: Record<string, (n: number) => string> = {
  practice: (n) => `The ${n} months before the exam.`,
  exam: (n) => `The last ${n} months, up to today.`,
  practice4: (n) => `The ${n} months before that exam.`,
  exam4: (n) => `The last ${n} months, up to today.`,
  stress: (n) => `The ${n} months before the practice (it ends at the April 2025 crash low).`,
  early: (n) => `The ${n} months before the stress check.`,
  bear: () => "The 2022 bear market: the S&P 500 fell about 25%. Run for the best setups only.",
};
// Windows only some tests have: not part of "All"
const PARTIAL_PERIODS = new Set(["bear"]);
/** "Practice · 9 mo": a window's name and its length */
const periodLabel = (k: string, p?: [string, string]) =>
  `${PERIOD_LABEL[k] ?? k.charAt(0).toUpperCase() + k.slice(1)}${p ? ` · ${months(p)} mo` : ""}`;
const periodTip = (k: string, p?: [string, string]) => PERIOD_TIP[k]?.(months(p)) ?? "";
const span = (p?: [string, string]) => (p ? `${p[0]} → ${p[1]}` : "");

/** The rules that differ between the tests of a sweep, as grid columns. */
const RULES: { key: string; title: string; tip: string; value: (c: DipPresetConfig) => number; text: (c: DipPresetConfig) => string }[] = [
  { key: "drop", title: "Fall", tip: "Buy after a fall of this much (or this many times the symbol's usual daily move) ...",
    value: (c) => (c.drop_mode === "volatility" ? 10 + c.drop_atr : c.drop_pct),
    text: (c) => (c.drop_mode === "volatility" ? `${+c.drop_atr.toFixed(2)}× move` : pctOf(c.drop_pct)) },
  { key: "lookback", title: "In", tip: "... under the highest close of the last this many trading days", value: (c) => c.lookback, text: (c) => `${c.lookback}d` },
  { key: "turn", title: "Turn", tip: "Wait for the turn: buy once it is back up this much from its low (off: buy at once)", value: (c) => (c.rebound ? c.rebound_pct : 0), text: (c) => (c.rebound ? pctOf(c.rebound_pct) : "off") },
  { key: "rise", title: "Sell", tip: "Sell when it is up this much from the buy", value: (c) => (c.target_mode === "percent" ? c.rise_pct : 0), text: (c) => (c.target_mode === "percent" ? `+${pctOf(c.rise_pct)}` : "back") },
  { key: "stop", title: "Stop", tip: "Stop-loss under the buy: sold and blacklisted", value: (c) => c.stop_pct, text: (c) => pctOf(c.stop_pct) },
  { key: "reenable", title: "Blacklist", tip: "A stopped-out symbol is blacklisted this many trading days", value: (c) => c.reenable_days ?? 0, text: (c) => `${c.reenable_days ?? 0}d` },
  { key: "hold", title: "Max hold", tip: "Sell anyway after this many trading days (— = never)", value: (c) => c.max_hold_days ?? 0, text: (c) => (c.max_hold_days ? `${c.max_hold_days}d` : "—") },
  { key: "trend", title: "Trend", tip: "Only buy dips of symbols in an uptrend (50-day average above the 200-day)", value: (c) => (c.trend_filter ? 1 : 0), text: (c) => (c.trend_filter ? "up only" : "—") },
  { key: "slots", title: "Slots", tip: "Positions at once: each buy gets 1/slots of the equity", value: (c) => c.max_positions, text: (c) => `${c.max_positions}` },
  { key: "cash", title: "Capital", tip: "Starting capital (fractional shares on)", value: (c) => c.initial_cash ?? 0, text: (c) => `$${c.initial_cash ?? "—"}` },
];

/** The grid's sorting and filters, done by the server: each column says how it is sorted and filtered right now. */
interface Controls {
  sortOrder: (key: string) => SortOrder;
  filtered: (key: string) => (string | number)[] | null;
}

function ruleColumn(r: (typeof RULES)[number], choices: { value: number; text: string }[], c: Controls): ColumnType<DipTest> {
  return {
    key: r.key,
    title: <Tooltip title={r.tip}>{r.title}</Tooltip>,
    width: r.key === "reenable" ? 76 : 66,
    align: "center",
    sorter: true,
    sortOrder: c.sortOrder(r.key),
    filters: choices.map((v) => ({ text: v.text, value: v.value })),
    filteredValue: c.filtered(r.key),
    render: (_, t) => r.text(t.config),
  };
}

function periodColumns(period: string, range: [string, string] | undefined, c: Controls): ColumnGroupType<DipTest> {
  const n = (t: DipTest): DipTestNumbers | undefined => t.results[period];
  return {
    key: period,
    title: <Tooltip title={`${periodTip(period, range)} ${span(range)}`}>{periodLabel(period, range)}</Tooltip>,
    children: [
      {
        key: `${period}-ret`,
        title: <Tooltip title="Its return, and holding all the symbols in equal parts under it">Return</Tooltip>,
        width: 92,
        align: "right",
        sorter: true,
        sortOrder: c.sortOrder(`${period}-ret`),
        render: (_: unknown, t: DipTest) => {
          const x = n(t);
          if (!x) return "—";
          if (x.error) return <Tooltip title={x.error}><Tag color="red">error</Tag></Tooltip>;
          return (
            <div style={{ lineHeight: 1.25 }}>
              <span style={{ color: color(x.total_return_pct), fontWeight: 600 }}>{signed(x.total_return_pct)}</span>
              <div style={{ fontSize: 11, color: x.total_return_pct > x.buy_hold_return_pct ? GREEN : MUTED }}>hold {signed(x.buy_hold_return_pct)}</div>
            </div>
          );
        },
      },
      {
        key: `${period}-dd`,
        title: <Tooltip title="The worst fall of its equity from a peak">Max DD</Tooltip>,
        width: 72,
        align: "right",
        sorter: true,
        sortOrder: c.sortOrder(`${period}-dd`),
        render: (_: unknown, t: DipTest) => (n(t) && !n(t)?.error ? <span style={{ color: (n(t)?.max_drawdown_pct ?? 0) < -15 ? RED : undefined }}>{signed(n(t)?.max_drawdown_pct)}</span> : "—"),
      },
      {
        key: `${period}-trades`,
        title: <Tooltip title="Round trips closed, the share that made money, and how many were stop-losses">Trades</Tooltip>,
        width: 84,
        align: "right",
        sorter: true,
        sortOrder: c.sortOrder(`${period}-trades`),
        render: (_: unknown, t: DipTest) => {
          const x = n(t);
          if (!x || x.error) return "—";
          return (
            <div style={{ lineHeight: 1.25 }}>
              {x.num_trades}
              <div style={{ fontSize: 11, color: MUTED }}>
                {x.win_rate_pct.toFixed(0)}% won{x.stop_exits ? <span style={{ color: RED }}> · {x.stop_exits} stop</span> : null}
              </div>
            </div>
          );
        },
      },
    ],
  };
}

/** One test opened up: the rules in words, then per window its equity against holding, and each symbol's trades. */
function TestDetailView({ test, periods }: { test: DipTest; periods: Record<string, [string, string]> }) {
  const [detail, setDetail] = useState<DipTestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    tradingApi.dipTest(test.id).then(setDetail, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [test.id]);
  const c = test.config;
  const r = pickRules(c);
  return (
    <div style={{ padding: "4px 8px" }}>
      <Typography.Paragraph style={{ marginBottom: 6 }}>
        Every {r.interval === "1d" ? "day" : r.interval}: buy {fallText(r)}; sell {sellText(r)}; stop-loss {pctOf(r.stop_pct)} under the buy, then the
        symbol is blacklisted for {c.reenable_days ?? 0} trading days. {r.max_positions} positions at once, each 1/{r.max_positions} of the equity
        {c.initial_cash ? ` (about $${Math.round(c.initial_cash / r.max_positions)} each at the start)` : ""}, {r.fractional ? "fractional shares" : "whole shares"}.
      </Typography.Paragraph>
      <div style={{ marginBottom: 8 }}>
        {c.symbols.map((s) => <Tag key={s}>{s}</Tag>)}
      </div>
      {test.extra && (
        <Alert type="warning" showIcon style={{ marginBottom: 8 }}
          message={`Plus a filter the bots don't have yet: ${test.extra}. Saved as a setup, it keeps the rules only.`} />
      )}
      {test.note && <Alert type="info" showIcon message={test.note} style={{ marginBottom: 8 }} />}
      {error && <Alert type="error" showIcon message={error} />}
      {!detail && !error && <Spin />}
      {detail && (
        <Row gutter={[12, 12]}>
          {Object.keys(periods).map((p) => {
            const d = detail.detail[p];
            const x = test.results[p];
            if (!d || !x) return null;
            const data = d.curve.map(([date, equity, hold]) => ({ date, equity, hold }));
            const traded = d.by_symbol.filter((s) => s.trades || s.status !== "watching");
            return (
              <Col key={p} xs={24} xl={Object.keys(periods).length > 2 ? 8 : 12}>
                <Card
                  size="small"
                  title={<span>{periodLabel(p, periods[p])} <span style={{ color: MUTED, fontWeight: 400, fontSize: 12 }}>{span(periods[p])}</span></span>}
                  extra={<span style={{ color: color(x.total_return_pct), fontWeight: 600 }}>{signed(x.total_return_pct)}</span>}
                >
                  <ResponsiveContainer width="100%" height={180}>
                    <LineChart data={data}>
                      <CartesianGrid stroke="#2a3550" />
                      <XAxis dataKey="date" tick={{ fill: MUTED, fontSize: 10 }} minTickGap={50} />
                      <YAxis tick={{ fill: MUTED, fontSize: 10 }} width={48} domain={["auto", "auto"]} />
                      <RTooltip contentStyle={{ background: "#182031", border: "1px solid #2a3550" }} labelStyle={{ color: "#e6ebf5" }}
                        formatter={(v: number) => `$${v.toFixed(2)}`} />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      <Line dataKey="equity" name="This setup" stroke="#4f8cff" dot={false} strokeWidth={2} isAnimationActive={false} />
                      <Line dataKey="hold" name="Buy & hold" stroke={MUTED} dot={false} strokeDasharray="5 4" isAnimationActive={false} />
                    </LineChart>
                  </ResponsiveContainer>
                  <div style={{ fontSize: 12, color: MUTED, margin: "4px 0" }}>
                    Avg trade {signed(x.avg_trade_pct, 2)} · worst {signed(x.worst_trade_pct)} · avg hold {x.avg_hold_days ?? "—"} days ·
                    invested {x.exposure_pct}% of days{x.open_position ? ` · ${x.open_position} still open at the end (${x.unrealized_pnl >= 0 ? "+" : ""}$${x.unrealized_pnl.toFixed(2)})` : ""}
                  </div>
                  <Table
                    size="small"
                    pagination={false}
                    rowKey="symbol"
                    dataSource={traded}
                    scroll={{ y: 180 }}
                    columns={[
                      { title: "Symbol", dataIndex: "symbol", width: 70 },
                      { title: "Trades", dataIndex: "trades", align: "right", sorter: (a, b) => a.trades - b.trades },
                      { title: "Won", dataIndex: "wins", align: "right" },
                      { title: "Stops", dataIndex: "stops", align: "right", render: (v: number) => (v ? <span style={{ color: RED }}>{v}</span> : 0) },
                      { title: "P&L", dataIndex: "pnl", align: "right", sorter: (a, b) => a.pnl - b.pnl, defaultSortOrder: "ascend",
                        render: (v: number) => <span style={{ color: color(v) }}>{v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(2)}</span> },
                    ]}
                  />
                </Card>
              </Col>
            );
          })}
        </Row>
      )}
    </div>
  );
}

/** Each rule's values and how they change the score, like for like (worked out by the server over the whole batch):
 *  among tests that are the same in every other rule (watchlist included), how far each value's score sits from the
 *  average of that group. Comparing plain medians would flatter values that were only tried around the best setups. */
function RuleEffects({ effects }: { effects: DipStudySummary["effects"] }) {
  const named: Record<string, { title: string; tip: string }> = {
    watchlist: { title: "Watchlist", tip: "" },
    extra: { title: "Filter", tip: "A filter tested on top of the rules" },
    ...Object.fromEntries(RULES.map((r) => [r.key, { title: r.title, tip: r.tip }])),
  };
  if (!effects.length) return null;
  return (
    <Card
      size="small"
      title={
        <Tooltip title="Like for like: among tests that differ only in this rule, how many points each value's score sits above (+) or below (−) their average. The score is a test's weaker return of practice and exam. A rule whose values are all near 0 hardly matters.">
          What matters: each rule's effect on the score
        </Tooltip>
      }
      style={{ marginBottom: 12 }}
    >
      {effects.map((g) => (
        <div key={g.key} style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap", marginBottom: 4 }}>
          <Tooltip title={named[g.key]?.tip}><span style={{ width: 80, color: MUTED, fontSize: 12, flex: "none" }}>{named[g.key]?.title ?? g.key}</span></Tooltip>
          {g.cells.map((c) => (
            <Tooltip key={c.label} title={`${c.n} tests compared`}>
              <Tag style={{ marginInlineEnd: 0 }}>
                {c.label} <b style={{ color: color(c.median) }}>{signed(c.median)}</b>
              </Tag>
            </Tooltip>
          ))}
        </div>
      ))}
    </Card>
  );
}

/** Every window back to back: what $1 grew to, minus 1, in % (null when a window is missing). */
function allWindows(t: DipTest, periods: string[]): number | null {
  let g = 1;
  for (const p of periods) {
    const x = t.results[p];
    if (!x || x.error) return null;
    g *= 1 + x.total_return_pct / 100;
  }
  return (g - 1) * 100;
}

/** Return per 1% of the worst drop, the weaker of practice and exam (null without both). */
function calm(t: DipTest): number | null {
  const xs = ["practice", "exam"].map((p) => t.results[p]).filter((x) => x && !x.error);
  if (xs.length < 2) return null;
  return Math.min(...xs.map((x) => x.total_return_pct / Math.max(1, -x.max_drawdown_pct)));
}

interface Props {
  /** Fill the backtest form with this setup (the page switches to the Backtest tab). */
  onLoad: (c: DipPresetConfig) => void;
}

const PAGE_SIZES = [25, 50, 100, 200];
const DEFAULT_SORT = { key: "pick", order: "asc" as const }; // the picks first, then the best score

export default function TestResults({ onLoad }: Props) {
  const { message } = AntApp.useApp();
  const [studies, setStudies] = useState<DipStudy[] | null>(null);
  const [studyId, setStudyId] = useState<number | null>(null);
  const [summary, setSummary] = useState<DipStudySummary | null>(null);
  const [rows, setRows] = useState<{ total: number; items: DipTest[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState<{ test: DipTest; name: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // What the server is asked for: the page, the sort, the filters (the switches, the search, each column's choices)
  const [page, setPage] = useState({ current: 1, size: 50 });
  const [sort, setSort] = useState<{ key: string; order: "asc" | "desc" }>(DEFAULT_SORT);
  const [filters, setFilters] = useState<Record<string, (string | number)[]>>({});
  const [onlyPicks, setOnlyPicks] = useState(false);
  const [bothPositive, setBothPositive] = useState(false);
  const [beatHold, setBeatHold] = useState(false);
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [more, setMore] = useState(false);
  const asked = useRef(0); // only the latest request's answer is shown
  const pin = !!Grid.useBreakpoint().md; // on a phone, pinned columns would fill the screen

  const loadPresets = useCallback(() => {
    tradingApi.dipPresets().then((list) => setSaved(new Set(list.map((p) => p.name.toLowerCase()))), () => undefined);
  }, []);

  useEffect(() => {
    tradingApi.dipStudies().then((list) => {
      setStudies(list);
      setStudyId((id) => id ?? list[0]?.id ?? null);
    }, (e) => setError(e instanceof Error ? e.message : String(e)));
    loadPresets();
  }, [loadPresets]);

  // Another batch: its own frame, and the grid from the top with nothing filtered
  useEffect(() => {
    if (studyId == null) return;
    setSummary(null);
    setRows(null);
    setPage((p) => ({ ...p, current: 1 }));
    setSort(DEFAULT_SORT);
    setFilters({});
    tradingApi.dipStudySummary(studyId).then(setSummary, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [studyId]);

  // Typing in the search box asks once it pauses
  useEffect(() => {
    const q = typed.trim();
    if (q === query) return;
    const t = setTimeout(() => {
      setQuery(q);
      setPage((p) => ({ ...p, current: 1 }));
    }, 300);
    return () => clearTimeout(t);
  }, [typed, query]);

  useEffect(() => {
    if (studyId == null) return;
    const ask: DipTestQuery = {
      offset: (page.current - 1) * page.size, limit: page.size, sort: sort.key, order: sort.order, q: query,
      picks: onlyPicks, profit_both: bothPositive, beat_both: beatHold, filters,
    };
    const n = ++asked.current;
    setLoading(true);
    tradingApi.dipTests(studyId, ask).then(
      (r) => { if (n === asked.current) setRows(r); },
      (e) => { if (n === asked.current) message.error(e instanceof Error ? e.message : String(e)); },
    ).finally(() => { if (n === asked.current) setLoading(false); });
  }, [studyId, page, sort, filters, query, onlyPicks, bothPositive, beatHold, message]);

  const toggle = (set: (v: boolean) => void) => (v: boolean) => {
    set(v);
    setPage((p) => ({ ...p, current: 1 }));
  };

  const study = studies?.find((s) => s.id === studyId) ?? null;
  const periods = study?.periods ?? {};
  const controls: Controls = {
    sortOrder: (key) => (sort.key === key ? (sort.order === "asc" ? "ascend" : "descend") : null),
    filtered: (key) => filters[key] ?? null,
  };

  const save = async () => {
    if (!saving) return;
    setBusy(true);
    try {
      await tradingApi.saveDipPreset(saving.name.trim(), saving.test.config);
      message.success(`Saved as “${saving.name.trim()}”: pick it under Saved setups`);
      setSaving(null);
      loadPresets();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const full = Object.keys(periods).filter((p) => !PARTIAL_PERIODS.has(p)); // the windows every test has
  // "All" chains the windows back to back: only when none of them overlap
  const chained = [...full].sort((a, b) => periods[a][0].localeCompare(periods[b][0]));
  const disjoint = chained.every((p, i) => i === 0 || periods[p][0] > periods[chained[i - 1]][1]);
  const columns: ColumnsType<DipTest> = [
    {
      key: "pick",
      title: <Tooltip title="The batch's recommendations, best first: how they were picked is in the description above">Pick</Tooltip>,
      width: 58,
      fixed: pin ? "left" : undefined,
      align: "center",
      sorter: true,
      sortOrder: controls.sortOrder("pick"),
      render: (_, t) => (t.pick ? <Tag color="gold" icon={<StarFilled />} style={{ marginInlineEnd: 0 }}>{t.pick}</Tag> : null),
    },
    {
      key: "name",
      title: "Setup",
      width: pin ? 300 : 200,
      fixed: pin ? "left" : undefined,
      sorter: true,
      sortOrder: controls.sortOrder("name"),
      render: (_, t) => (
        <span>
          {t.name}
          {saved.has(t.name.toLowerCase()) && <Tag color="green" style={{ marginInlineStart: 6 }}>saved</Tag>}
        </span>
      ),
    },
    {
      key: "watchlist",
      title: "Watchlist",
      width: 130,
      filters: (summary?.watchlists ?? []).map((w) => ({ text: w, value: w })),
      filteredValue: controls.filtered("watchlist"),
      sorter: true,
      sortOrder: controls.sortOrder("watchlist"),
      render: (_, t) => <Tooltip title={t.config.symbols.join(" ")}>{t.watchlist} <span style={{ color: MUTED }}>({t.config.symbols.length})</span></Tooltip>,
    },
    {
      key: "score",
      title: <Tooltip title="The weaker of its practice and exam returns: a setup is only as good as its worse window">Score</Tooltip>,
      width: 74,
      align: "right",
      sorter: true,
      sortOrder: controls.sortOrder("score"),
      render: (_, t) => <b style={{ color: color(t.score) }}>{signed(t.score)}</b>,
    },
    {
      key: "calm",
      title: <Tooltip title="Steadiness: its return per 1% of its worst drop, the weaker of practice and exam. Higher = more return for the bumps along the way (2 = it made twice its worst drop)">Ret/DD</Tooltip>,
      width: 70,
      align: "right",
      sorter: true,
      sortOrder: controls.sortOrder("calm"),
      render: (_, t) => {
        const c = calm(t);
        return c == null ? "—" : <span style={{ color: c >= 1 ? GREEN : c < 0 ? RED : undefined }}>{c.toFixed(2)}</span>;
      },
    },
    ...(full.length > 2 && disjoint
      ? [{
          key: "all",
          title: <Tooltip title={`All ${full.length} windows back to back (${full.reduce((n, p) => n + months(periods[p]), 0)} months): what it grew to`}>All</Tooltip>,
          width: 78,
          align: "right" as const,
          sorter: true,
          sortOrder: controls.sortOrder("all"),
          render: (_: unknown, t: DipTest) => {
            const v = allWindows(t, full);
            return <b style={{ color: color(v) }}>{signed(v, 0)}</b>;
          },
        }]
      : []),
    ...(summary?.has_quarters
      ? [{
          key: "quarters",
          title: <Tooltip title="Twelve separate 3-month tests, Oct 2023 to Oct 2026: how many made money, and the worst one. Run for the best setups only">Quarters</Tooltip>,
          width: 92,
          align: "right" as const,
          sorter: true,
          sortOrder: controls.sortOrder("quarters"),
          render: (_: unknown, t: DipTest) => {
            const q = t.quarters;
            if (!q) return <span style={{ color: MUTED }}>—</span>;
            return (
              <Tooltip title={q.returns.map((r, i) => `Q${i + 1} ${signed(r)}`).join(" · ")}>
                <div style={{ lineHeight: 1.25 }}>
                  <b style={{ color: q.won >= q.of * 0.75 ? GREEN : q.won < q.of / 2 ? RED : undefined }}>{q.won}/{q.of}</b>
                  <div style={{ fontSize: 11, color: MUTED }}>worst {signed(q.worst)}</div>
                </div>
              </Tooltip>
            );
          },
        }]
      : []),
    ...Object.keys(periods).map((p) => periodColumns(p, periods[p], controls)),
    ...(summary?.extras.length
      ? [{
          key: "extra",
          title: <Tooltip title="A filter tested on top of the rules. The bots don't have it yet: a saved setup keeps the rules only">Filter</Tooltip>,
          width: 150,
          filters: summary.extras.map((v) => ({ text: v, value: v })),
          filteredValue: controls.filtered("extra"),
          render: (_: unknown, t: DipTest) => (t.extra ? <Tag color="purple" style={{ whiteSpace: "normal" }}>{t.extra}</Tag> : <span style={{ color: MUTED }}>—</span>),
        }]
      : []),
    // Only the rules this batch varied
    ...RULES.filter((r) => summary?.rules[r.key]).map((r) => ruleColumn(r, summary!.rules[r.key], controls)),
    {
      key: "actions",
      title: "",
      width: 76,
      fixed: pin ? "right" : undefined,
      render: (_, t) => (
        <Space size={2}>
          <Tooltip title="Save as a setup (then pick it under Saved setups: backtest, new bot, a bot's rules)">
            <Button size="small" type="text" icon={<SaveOutlined />} onClick={() => setSaving({ test: t, name: t.name })} />
          </Tooltip>
          <Tooltip title="Load into the backtest form">
            <Button size="small" type="text" icon={<ExperimentOutlined />} onClick={() => onLoad({ ...t.config, ...pickRules(t.config) })} />
          </Tooltip>
        </Space>
      ),
    },
  ];

  if (error) return <Alert type="error" showIcon message={`Can't load the test results: ${error}`} />;
  if (!studies) return <Spin />;
  if (!studies.length) return <Card><Empty description={<span style={{ color: MUTED }}>No test batches yet.</span>} /></Card>;

  return (
    <>
      <Card size="small" style={{ marginBottom: 12 }}>
        {/* The batch on a line of its own, never wider than the card (a phone): the box cuts a long name short, the
            list wraps it (a date's hyphens don't break) */}
        {studies.length > 1 ? (
          <Select value={studyId} onChange={setStudyId} style={{ width: "100%", maxWidth: 520, marginBottom: 8 }}
            options={studies.map((s) => ({ value: s.id, label: `${s.name} (${s.tests})` }))}
            optionRender={(o) => <div style={{ whiteSpace: "normal" }}>{String(o.label).replace(/(\d)-(\d)/g, "$1\u2011$2")}</div>} />
        ) : (
          <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>{study?.name}</Typography.Text>
        )}
        <Space wrap size={[8, 6]} style={{ marginBottom: 8 }}>
          {Object.entries(periods).map(([k, v]) => (
            <Tooltip key={k} title={periodTip(k, v)}>
              <Tag style={{ whiteSpace: "normal", marginInlineEnd: 0 }}>{periodLabel(k, v)}: {span(v)}</Tag>
            </Tooltip>
          ))}
        </Space>
        {study?.description && (
          <Typography.Paragraph style={{ whiteSpace: "pre-wrap", fontSize: 13, marginBottom: 0 }}>
            {more ? study.description : study.description.split("\n\n")[0]}{" "}
            {study.description.includes("\n\n") && <Typography.Link onClick={() => setMore(!more)}>{more ? "Less" : "More: how it was tested, what came out, cautions"}</Typography.Link>}
          </Typography.Paragraph>
        )}
      </Card>
      {summary && (
        <>
          <Row gutter={12} style={{ marginBottom: 4 }}>
            <Col>
              <Typography.Text style={{ color: MUTED, fontSize: 12 }}>
                {summary.tests} setups tested · {summary.made_money_both} made money in both practice and exam · {summary.beat_hold_both} also beat
                buy & hold in both
              </Typography.Text>
            </Col>
          </Row>
          <RuleEffects effects={summary.effects} />
          <Space wrap style={{ marginBottom: 8 }}>
            <Input.Search allowClear placeholder="Find a setup or symbol" value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: 220 }} />
            <span><Switch size="small" checked={onlyPicks} onChange={toggle(setOnlyPicks)} /> Picks only</span>
            <span><Switch size="small" checked={bothPositive} onChange={toggle(setBothPositive)} /> Made money in practice and exam</span>
            <span><Switch size="small" checked={beatHold} onChange={toggle(setBeatHold)} /> Beat buy & hold in both</span>
            <span style={{ color: MUTED, fontSize: 12 }}>{rows ? `${rows.total} shown` : ""}</span>
          </Space>
        </>
      )}
      <Table<DipTest>
        size="small"
        rowKey="id"
        loading={loading || !rows}
        dataSource={rows?.items ?? []}
        columns={columns}
        scroll={{ x: "max-content" }}
        sticky
        pagination={{ current: page.current, pageSize: page.size, total: rows?.total ?? 0, pageSizeOptions: PAGE_SIZES, showSizeChanger: true }}
        onChange={(p, f, sorter, { action }) => {
          // One request for whatever changed; a new sort or filter starts from the first page
          if (action === "paginate") {
            setPage({ current: p.current ?? 1, size: p.pageSize ?? page.size });
            return;
          }
          const s = Array.isArray(sorter) ? sorter[0] : sorter;
          setSort(s?.order ? { key: String(s.columnKey), order: s.order === "ascend" ? "asc" : "desc" } : DEFAULT_SORT);
          setFilters(Object.fromEntries(Object.entries(f).filter(([, v]) => v?.length)) as Record<string, (string | number)[]>);
          setPage((old) => ({ ...old, current: 1 }));
        }}
        expandable={{ expandedRowRender: (t) => <TestDetailView test={t} periods={periods} /> }}
      />
      <Modal
        open={!!saving}
        title="Save as a setup"
        okText={saving && saved.has(saving.name.trim().toLowerCase()) ? "Replace" : "Save"}
        confirmLoading={busy}
        okButtonProps={{ disabled: !saving?.name.trim() }}
        onOk={save}
        onCancel={() => setSaving(null)}
      >
        <Input value={saving?.name} maxLength={60} onChange={(e) => saving && setSaving({ ...saving, name: e.target.value })} onPressEnter={save} />
        <Typography.Paragraph style={{ color: MUTED, fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          Its rules, watchlist, capital, blacklist days and “the last 9 months” periods. Pick it under Saved setups in the backtest, a new dip bot or a bot's rules.
          {saving && saved.has(saving.name.trim().toLowerCase()) && " A setup with this name exists: saving replaces it."}
          {saving?.test.extra && ` Not its filter (${saving.test.extra}): the bots and the backtest form don't have it yet.`}
        </Typography.Paragraph>
      </Modal>
    </>
  );
}
