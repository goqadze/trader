// A strategy's test batches (parameter sweeps): every tested setup under its name, its rules and how it did in each
// window, as a sortable grid, filterable by its symbols' sectors too. Any of them can be saved as a setup, loaded into
// the backtest form or started as a bot. The dip buyer's and
// the momentum rotation's (each strategy's rules and words come from its kit: dip/kit.tsx, rotation/kit.tsx).
// trading-service keeps them (/dip/studies?strategy=, /dip/tests); a sweep script fills them in.

import { ExperimentOutlined, RocketOutlined, SaveOutlined, StarFilled } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, Empty, Grid, Input, Modal, Row, Select, Space, Spin, Switch, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnGroupType, ColumnsType, ColumnType, SortOrder } from "antd/es/table/interface";
import dayjs from "dayjs";
import { useCallback, useEffect, useRef, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip as RTooltip, XAxis, YAxis } from "recharts";
import { tradingApi } from "../trading/api";
import { SectorTags } from "../trading/sectors";
import { PERIOD_LABEL, type RuleColumn, type StrategyKit } from "./kit";
import type { Study, StudySummary, StudyTest, StudyTestDetail, TestNumbers, TestQuery } from "./types";

const MUTED = "#8b98b5";
const GREEN = "#33c088";
const RED = "#ef5b6b";
const color = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? undefined : v > 0 ? GREEN : RED);
const signed = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);

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
const periodTip = (k: string, p?: [string, string], tips?: Record<string, (n: number) => string>) =>
  (tips?.[k] ?? PERIOD_TIP[k])?.(months(p)) ?? "";
const span = (p?: [string, string]) => (p ? `${p[0]} → ${p[1]}` : "");

/** The grid's sorting and filters, done by the server: each column says how it is sorted and filtered right now. */
interface Controls {
  sortOrder: (key: string) => SortOrder;
  filtered: (key: string) => (string | number)[] | null;
}

function ruleColumn<C>(r: RuleColumn<C>, choices: { value: number; text: string }[], c: Controls): ColumnType<StudyTest<C>> {
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

function periodColumns<C>(period: string, range: [string, string] | undefined, c: Controls, tip: string): ColumnGroupType<StudyTest<C>> {
  const n = (t: StudyTest<C>): TestNumbers | undefined => t.results[period];
  return {
    key: period,
    title: <Tooltip title={`${tip} ${span(range)}`}>{periodLabel(period, range)}</Tooltip>,
    children: [
      {
        key: `${period}-ret`,
        title: <Tooltip title="Its return, and holding all the symbols in equal parts under it">Return</Tooltip>,
        width: 92,
        align: "right",
        sorter: true,
        sortOrder: c.sortOrder(`${period}-ret`),
        render: (_: unknown, t: StudyTest<C>) => {
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
        render: (_: unknown, t: StudyTest<C>) => (n(t) && !n(t)?.error ? <span style={{ color: (n(t)?.max_drawdown_pct ?? 0) < -15 ? RED : undefined }}>{signed(n(t)?.max_drawdown_pct)}</span> : "—"),
      },
      {
        key: `${period}-trades`,
        title: <Tooltip title="Round trips closed, the share that made money, and how many were stop-losses">Trades</Tooltip>,
        width: 84,
        align: "right",
        sorter: true,
        sortOrder: c.sortOrder(`${period}-trades`),
        render: (_: unknown, t: StudyTest<C>) => {
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
function TestDetailView<C extends { symbols: string[] }>({ test, periods, kit }: {
  test: StudyTest<C>; periods: Record<string, [string, string]>; kit: StrategyKit<C>;
}) {
  const [detail, setDetail] = useState<StudyTestDetail<C> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    tradingApi.studyTest<C>(test.id).then(setDetail, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [test.id]);
  const c = test.config;
  return (
    <div style={{ padding: "4px 8px" }}>
      <Typography.Paragraph style={{ marginBottom: 6 }}>{kit.describe(c)}</Typography.Paragraph>
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
                      ...(kit.stops ? [{ title: "Stops", dataIndex: "stops", align: "right" as const, render: (v: number) => (v ? <span style={{ color: RED }}>{v}</span> : 0) }] : []),
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
function RuleEffects<C>({ effects, rules, listWord }: { effects: StudySummary["effects"]; rules: RuleColumn<C>[]; listWord: string }) {
  const named: Record<string, { title: string; tip: string }> = {
    watchlist: { title: listWord.charAt(0).toUpperCase() + listWord.slice(1), tip: "" },
    extra: { title: "Filter", tip: "A filter tested on top of the rules" },
    ...Object.fromEntries(rules.map((r) => [r.key, { title: r.title, tip: r.tip }])),
  };
  if (!effects.length) return null;
  return (
    <Card
      size="small"
      title={
        <Tooltip title={`Like for like: among tests that differ only in this rule (the ${listWord} too), how many points each value's score sits above (+) or below (−) their average. The score is a test's weaker return of practice and exam. A rule whose values are all near 0 hardly matters.`}>
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
function allWindows(t: StudyTest<unknown>, periods: string[]): number | null {
  let g = 1;
  for (const p of periods) {
    const x = t.results[p];
    if (!x || x.error) return null;
    g *= 1 + x.total_return_pct / 100;
  }
  return (g - 1) * 100;
}

/** Return per 1% of the worst drop, the weaker of practice and exam (null without both). */
function calm(t: StudyTest<unknown>): number | null {
  const xs = ["practice", "exam"].map((p) => t.results[p]).filter((x) => x && !x.error);
  if (xs.length < 2) return null;
  return Math.min(...xs.map((x) => x.total_return_pct / Math.max(1, -x.max_drawdown_pct)));
}

interface Props<C extends { symbols: string[] }> {
  kit: StrategyKit<C>;
  /** Fill the backtest form with this setup (the page switches to the Backtest tab). */
  onLoad: (c: C) => void;
  /** Open the page's new bot form filled in with this setup, named after it; the bot keeps a copy of test `testId`. */
  onStartBot: (c: C, name: string, testId: number) => void;
}

const PAGE_SIZES = [25, 50, 100, 200];
// A touch screen opens a hover hint on the tap that also opens a dialog, and leaves it on top of the dialog: the row's
// buttons get their hints only where there is a mouse
const HOVER = window.matchMedia?.("(hover: hover)").matches ?? true;
const hint = (text: string) => (HOVER ? text : undefined);
const DEFAULT_SORT = { key: "pick", order: "asc" as const }; // the picks first, then the best score

export default function TestResults<C extends { symbols: string[] }>({ kit, onLoad, onStartBot }: Props<C>) {
  const { message } = AntApp.useApp();
  const [studies, setStudies] = useState<Study[] | null>(null);
  const [studyId, setStudyId] = useState<number | null>(null);
  const [summary, setSummary] = useState<StudySummary | null>(null);
  const [rows, setRows] = useState<{ total: number; items: StudyTest<C>[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState<{ test: StudyTest<C>; name: string } | null>(null);
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
    kit.presets.list().then((list) => setSaved(new Set(list.map((p) => p.name.toLowerCase()))), () => undefined);
  }, [kit]);

  useEffect(() => {
    tradingApi.studies(kit.strategy).then((list) => {
      setStudies(list);
      setStudyId((id) => id ?? list[0]?.id ?? null);
    }, (e) => setError(e instanceof Error ? e.message : String(e)));
    loadPresets();
  }, [kit, loadPresets]);

  // Another batch: its own frame, and the grid from the top with nothing filtered
  useEffect(() => {
    if (studyId == null) return;
    setSummary(null);
    setRows(null);
    setPage((p) => ({ ...p, current: 1 }));
    setSort(DEFAULT_SORT);
    setFilters({});
    tradingApi.studySummary(studyId).then(setSummary, (e) => setError(e instanceof Error ? e.message : String(e)));
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
    const ask: TestQuery = {
      offset: (page.current - 1) * page.size, limit: page.size, sort: sort.key, order: sort.order, q: query,
      picks: onlyPicks, profit_both: bothPositive, beat_both: beatHold, filters,
    };
    const n = ++asked.current;
    setLoading(true);
    tradingApi.studyTests<C>(studyId, ask).then(
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

  /** The bot gets the rules, not a tested filter on top of them (the bots don't have those yet). */
  const startBot = (t: StudyTest<C>) => {
    if (t.extra) message.warning(`The bot gets the rules, not the filter (${t.extra}): the bots don't have it yet`);
    onStartBot(kit.complete(t.config), t.name.slice(0, 80), t.id);
  };

  const save = async () => {
    if (!saving) return;
    setBusy(true);
    try {
      await kit.presets.save(saving.name.trim(), kit.complete(saving.test.config), saving.test.id);
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
  const columns: ColumnsType<StudyTest<C>> = [
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
      title: kit.listWord.charAt(0).toUpperCase() + kit.listWord.slice(1),
      width: 130,
      filters: (summary?.watchlists ?? []).map((w) => ({ text: w, value: w })),
      filteredValue: controls.filtered("watchlist"),
      sorter: true,
      sortOrder: controls.sortOrder("watchlist"),
      render: (_, t) => <Tooltip title={t.config.symbols.join(" ")}>{t.watchlist} <span style={{ color: MUTED }}>({t.config.symbols.length})</span></Tooltip>,
    },
    {
      key: "sector",
      title: <Tooltip title={`The sectors of its ${kit.listWord}, as a bot with it would get them: a sector holding at least a fifth of its symbols`}>Sectors</Tooltip>,
      width: 150,
      filters: (summary?.sectors ?? []).map((s) => ({ text: s, value: s })),
      filteredValue: controls.filtered("sector"),
      render: (_, t) => <SectorTags sectors={t.sectors ?? []} />,
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
          render: (_: unknown, t: StudyTest<C>) => {
            const v = allWindows(t, full);
            return <b style={{ color: color(v) }}>{signed(v, 0)}</b>;
          },
        }]
      : []),
    ...(summary?.has_quarters
      ? [{
          key: "quarters",
          title: <Tooltip title={kit.quarters.tip}>{kit.quarters.title}</Tooltip>,
          width: 92,
          align: "right" as const,
          sorter: true,
          sortOrder: controls.sortOrder("quarters"),
          render: (_: unknown, t: StudyTest<C>) => {
            const q = t.quarters;
            if (!q) return <span style={{ color: MUTED }}>—</span>;
            return (
              <Tooltip title={q.returns.map((r, i) => `${kit.quarters.short}${i + 1} ${signed(r)}`).join(" · ")}>
                <div style={{ lineHeight: 1.25 }}>
                  <b style={{ color: q.won >= q.of * 0.75 ? GREEN : q.won < q.of / 2 ? RED : undefined }}>{q.won}/{q.of}</b>
                  <div style={{ fontSize: 11, color: MUTED }}>worst {signed(q.worst)}</div>
                </div>
              </Tooltip>
            );
          },
        }]
      : []),
    ...Object.keys(periods).map((p) => periodColumns<C>(p, periods[p], controls, periodTip(p, periods[p], kit.periodTips))),
    ...(summary?.extras.length
      ? [{
          key: "extra",
          title: <Tooltip title="A filter tested on top of the rules. The bots don't have it yet: a saved setup keeps the rules only">Filter</Tooltip>,
          width: 150,
          filters: summary.extras.map((v) => ({ text: v, value: v })),
          filteredValue: controls.filtered("extra"),
          render: (_: unknown, t: StudyTest<C>) => (t.extra ? <Tag color="purple" style={{ whiteSpace: "normal" }}>{t.extra}</Tag> : <span style={{ color: MUTED }}>—</span>),
        }]
      : []),
    // Only the rules this batch varied
    ...kit.rules.filter((r) => summary?.rules[r.key]).map((r) => ruleColumn(r, summary!.rules[r.key], controls)),
    {
      key: "actions",
      title: "",
      width: 104,
      fixed: pin ? "right" : undefined,
      render: (_, t) => (
        <Space size={2}>
          <Tooltip title={hint("Save as a setup (then pick it under Saved setups: the backtest, a new bot)")}>
            <Button size="small" type="text" icon={<SaveOutlined />} aria-label="Save as a setup" onClick={() => setSaving({ test: t, name: t.name })} />
          </Tooltip>
          <Tooltip title={hint("Load into the backtest form")}>
            <Button size="small" type="text" icon={<ExperimentOutlined />} aria-label="Load into the backtest form" onClick={() => onLoad(kit.complete(t.config))} />
          </Tooltip>
          <Tooltip title={hint("Start a bot with this setup (opens the new bot form, filled in: pick the broker and check it first)")}>
            <Button size="small" type="text" icon={<RocketOutlined />} aria-label="Start a bot with this setup" onClick={() => startBot(t)} />
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
            <Tooltip key={k} title={periodTip(k, v, kit.periodTips)}>
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
          <RuleEffects effects={summary.effects} rules={kit.rules} listWord={kit.listWord} />
          <Space wrap style={{ marginBottom: 8 }}>
            <Input.Search allowClear placeholder="Find a setup or symbol" value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: 220 }} />
            <span><Switch size="small" checked={onlyPicks} onChange={toggle(setOnlyPicks)} /> Picks only</span>
            <span><Switch size="small" checked={bothPositive} onChange={toggle(setBothPositive)} /> Made money in practice and exam</span>
            <span><Switch size="small" checked={beatHold} onChange={toggle(setBeatHold)} /> Beat buy & hold in both</span>
            <span style={{ color: MUTED, fontSize: 12 }}>{rows ? `${rows.total} shown` : ""}</span>
          </Space>
        </>
      )}
      <Table<StudyTest<C>>
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
        expandable={{ expandedRowRender: (t) => <TestDetailView test={t} periods={periods} kit={kit} /> }}
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
          Keeps {kit.keeps}. Pick it under Saved setups in the backtest or a new bot.
          {saving && saved.has(saving.name.trim().toLowerCase()) && " A setup with this name exists: saving replaces it."}
          {saving?.test.extra && ` Not its filter (${saving.test.extra}): the bots and the backtest form don't have it yet.`}
        </Typography.Paragraph>
      </Modal>
    </>
  );
}
