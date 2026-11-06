// The setups saved from Test results (or from a bot's tested setup), from every batch together: each keeps a copy of
// its test made when it was saved (the batch, its windows, the rules, how it did), so setups from different sweeps can
// be compared side by side, loaded into the backtest form or started as a bot. A window of the same name can have other
// dates in another batch: each row's are in its cells' hints. Sorted and filtered here: there are a few dozen at most.

import { DeleteOutlined, ExperimentOutlined, RocketOutlined, StarFilled } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Empty, Grid, Input, Popconfirm, Space, Spin, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnGroupType, ColumnsType, ColumnType, FilterValue } from "antd/es/table/interface";
import dayjs from "dayjs";
import { useCallback, useEffect, useMemo, useState } from "react";
import { SectorTags } from "../trading/sectors";
import type { SetupNumbers } from "../trading/types";
import { PERIOD_LABEL, type RuleColumn, type StrategyKit } from "./kit";
import { TestDetailView, color, months, signed, span } from "./TestResults";
import type { SavedSetup } from "./types";

const MUTED = "#8b98b5";
const GREEN = "#33c088";
const RED = "#ef5b6b";
// A touch screen leaves a hover hint on top of the dialog its tap opens: the row's buttons get hints only with a mouse
const HOVER = window.matchMedia?.("(hover: hover)").matches ?? true;
const hint = (text: string) => (HOVER ? text : undefined);

type Tested<C> = SavedSetup<C> & { setup: NonNullable<SavedSetup<C>["setup"]> };
/** A saved test and its setup with any rule added since it was saved filled in (what Load and Start a bot use). */
interface Row<C> {
  saved: Tested<C>;
  config: C;
}

const windowName = (k: string) => PERIOD_LABEL[k] ?? k.charAt(0).toUpperCase() + k.slice(1);
/** "Dip sweep 8" out of "Dip sweep 8 · stops 4-15%, … · 2026-10-23": a batch's name before its details */
const shortStudy = (study: string) => study.split(" · ")[0];
const studyDate = (study: string) => /\d{4}-\d{2}-\d{2}$/.exec(study)?.[0] ?? "";
/** Numbers sort with the missing ones below every real one */
const by = <T,>(f: (x: T) => number | null | undefined) => (a: T, b: T) => (f(a) ?? -Infinity) - (f(b) ?? -Infinity);

/** Return per 1% of the worst drop, the weaker of practice and exam (Test results' Ret/DD; null without both). */
function calm(results: Record<string, SetupNumbers>): number | null {
  const xs = ["practice", "exam"].map((p) => results[p]);
  if (xs.some((x) => x?.total_return_pct == null || x.max_drawdown_pct == null)) return null;
  return Math.min(...xs.map((x) => x!.total_return_pct! / Math.max(1, -x!.max_drawdown_pct!)));
}

function windowColumns<C>(k: string): ColumnGroupType<Row<C>> {
  const n = (r: Row<C>): SetupNumbers | undefined => r.saved.setup.results[k];
  const where = (r: Row<C>) => {
    const p = r.saved.setup.periods[k];
    return p ? `${shortStudy(r.saved.setup.study)}'s ${windowName(k).toLowerCase()}: ${span(p)} (${months(p)} months)` : "";
  };
  return {
    key: k,
    title: (
      <Tooltip title={`Each batch's own ${windowName(k).toLowerCase()} window: hover a return for its dates. Windows of different lengths aren't like for like.`}>
        {windowName(k)}
      </Tooltip>
    ),
    children: [
      {
        key: `${k}-ret`,
        title: <Tooltip title="Its return, and holding all the symbols in equal parts under it">Return</Tooltip>,
        width: 92,
        align: "right",
        sorter: by((r: Row<C>) => n(r)?.total_return_pct),
        render: (_: unknown, r: Row<C>) => {
          const x = n(r);
          if (x?.total_return_pct == null) return <Tooltip title={`${shortStudy(r.saved.setup.study)} didn't test this window`}><span style={{ color: MUTED }}>—</span></Tooltip>;
          const beat = x.buy_hold_return_pct != null && x.total_return_pct > x.buy_hold_return_pct;
          return (
            <Tooltip title={where(r)}>
              <div style={{ lineHeight: 1.25 }}>
                <span style={{ color: color(x.total_return_pct), fontWeight: 600 }}>{signed(x.total_return_pct)}</span>
                <div style={{ fontSize: 11, color: beat ? GREEN : MUTED }}>hold {signed(x.buy_hold_return_pct)}</div>
              </div>
            </Tooltip>
          );
        },
      },
      {
        key: `${k}-dd`,
        title: <Tooltip title="The worst fall of its equity from a peak">Max DD</Tooltip>,
        width: 72,
        align: "right",
        sorter: by((r: Row<C>) => n(r)?.max_drawdown_pct),
        render: (_: unknown, r: Row<C>) => {
          const dd = n(r)?.max_drawdown_pct;
          return dd == null ? "—" : <span style={{ color: dd < -15 ? RED : undefined }}>{signed(dd)}</span>;
        },
      },
      {
        key: `${k}-trades`,
        title: <Tooltip title="Round trips closed, the share that made money, and how many were stop-losses">Trades</Tooltip>,
        width: 84,
        align: "right",
        sorter: by((r: Row<C>) => n(r)?.num_trades),
        render: (_: unknown, r: Row<C>) => {
          const x = n(r);
          if (x?.num_trades == null) return "—";
          return (
            <div style={{ lineHeight: 1.25 }}>
              {x.num_trades}
              <div style={{ fontSize: 11, color: MUTED }}>
                {x.win_rate_pct != null ? `${x.win_rate_pct.toFixed(0)}% won` : ""}{x.stop_exits ? <span style={{ color: RED }}> · {x.stop_exits} stop</span> : null}
              </div>
            </div>
          );
        },
      },
    ],
  };
}

function ruleColumn<C>(r: RuleColumn<C>, values: Map<number, string>, filtered: Filters): ColumnType<Row<C>> {
  return {
    key: r.key,
    title: <Tooltip title={r.tip}>{r.title}</Tooltip>,
    width: r.key === "reenable" ? 76 : 66,
    align: "center",
    sorter: by((x: Row<C>) => r.value(x.config)),
    filters: [...values].sort((a, b) => a[0] - b[0]).map(([value, text]) => ({ text, value })),
    filteredValue: filtered[r.key] ?? null,
    render: (_, x) => r.text(x.config),
  };
}

/** Each column's chosen filter values (column key -> the values kept): applied here, so "n shown" counts them too. */
type Filters = Record<string, FilterValue | null>;

interface Props<C extends { symbols: string[] }> {
  kit: StrategyKit<C>;
  /** The tab is showing: the saved setups are read again (one saved on Test results since then shows up). */
  active: boolean;
  /** Fill the backtest form with this setup (the page switches to the Backtest tab). */
  onLoad: (c: C) => void;
  /** Open the page's new bot form filled in with this setup; the bot keeps the saved copy of its test. */
  onStartBot: (c: C, saved: SavedSetup<C>) => void;
}

export default function SavedTests<C extends { symbols: string[] }>({ kit, active, onLoad, onStartBot }: Props<C>) {
  const { message } = AntApp.useApp();
  const [all, setAll] = useState<SavedSetup<C>[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");
  const [filters, setFilters] = useState<Filters>({});
  const pin = !!Grid.useBreakpoint().md; // on a phone, pinned columns would fill the screen

  const load = useCallback(() => {
    kit.presets.list().then((list) => { setAll(list); setError(null); }, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [kit]);
  useEffect(() => {
    if (active) load();
  }, [active, load]);

  const rows = useMemo<Row<C>[]>(
    () => (all ?? []).filter((p): p is Tested<C> => !!p.setup).map((saved) => ({ saved, config: kit.complete(saved.config) })),
    [all, kit],
  );
  const fromForms = (all ?? []).filter((p) => !p.setup);
  // The windows any of them has, in their batches' order (practice and exam first)
  const windows = [...new Set(rows.flatMap((r) => Object.keys(r.saved.setup.periods)))];
  const studies = [...new Set(rows.map((r) => r.saved.setup.study))];
  const watchlists = [...new Set(rows.map((r) => r.saved.setup.watchlist).filter(Boolean))].sort();
  const sectors = [...new Set(rows.flatMap((r) => r.saved.sectors ?? []))].sort();
  const extras = [...new Set(rows.map((r) => r.saved.setup.extra).filter((x): x is string => !!x))];
  // Only the rules they differ in
  const rules = kit.rules
    .map((r) => ({ r, values: new Map(rows.map((x) => [r.value(x.config), r.text(x.config)])) }))
    .filter((x) => x.values.size > 1);

  const q = typed.trim().toLowerCase();
  const keeps = (key: string, ...values: (string | number)[]) => !filters[key]?.length || values.some((v) => filters[key]!.includes(v));
  const shown = rows.filter(({ saved: p, config }) =>
    (!q || [p.name, p.setup.code, p.setup.name, p.setup.watchlist, p.setup.study].some((s) => s.toLowerCase().includes(q))
      || config.symbols.some((s) => s.toLowerCase() === q))
    && keeps("study", p.setup.study) && keeps("watchlist", p.setup.watchlist) && keeps("sector", ...(p.sectors ?? []))
    && keeps("extra", p.setup.extra ?? "") && rules.every(({ r }) => keeps(r.key, r.value(config))));

  const remove = async (p: Tested<C>) => {
    try {
      await kit.presets.remove(p.id);
      message.success(`Deleted the saved “${p.name}”`);
      load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  /** The bot gets the rules, not a tested filter on top of them (the bots don't have those yet). */
  const startBot = ({ saved: p, config }: Row<C>) => {
    if (p.setup.extra) message.warning(`The bot gets the rules, not the filter (${p.setup.extra}): the bots don't have it yet`);
    onStartBot(config, p);
  };

  const extraColumn: ColumnType<Row<C>> = {
    key: "extra",
    title: <Tooltip title="A filter tested on top of the rules. The bots don't have it yet: a bot or the backtest gets the rules only">Filter</Tooltip>,
    width: 150,
    filters: [{ text: "—", value: "" }, ...extras.map((v) => ({ text: v, value: v }))],
    filteredValue: filters.extra ?? null,
    render: (_, { saved: p }) =>
      p.setup.extra ? <Tag color="purple" style={{ whiteSpace: "normal" }}>{p.setup.extra}</Tag> : <span style={{ color: MUTED }}>—</span>,
  };

  const columns: ColumnsType<Row<C>> = [
    {
      key: "name",
      title: "Setup",
      width: pin ? 280 : 190,
      fixed: pin ? "left" : undefined,
      sorter: (a, b) => a.saved.name.localeCompare(b.saved.name),
      render: (_, { saved: p }) => (
        <span>
          {p.name}
          {!p.name.includes(p.setup.code) && <Tag color="geekblue" style={{ marginInlineStart: 6 }}>{p.setup.code}</Tag>}
          {p.setup.pick != null && !p.name.toLowerCase().includes("pick") && (
            <Tooltip title={`Pick ${p.setup.pick} of its batch`}><Tag color="gold" icon={<StarFilled />} style={{ marginInlineStart: 6 }}>{p.setup.pick}</Tag></Tooltip>
          )}
        </span>
      ),
    },
    {
      key: "study",
      title: <Tooltip title="The batch (test sweep) it was saved from">Batch</Tooltip>,
      width: 120,
      filters: studies.map((s) => ({ text: s, value: s })),
      filteredValue: filters.study ?? null,
      sorter: (a, b) => a.saved.setup.study_id - b.saved.setup.study_id,
      render: (_, { saved: p }) => (
        <Tooltip title={p.setup.study}>
          <div style={{ lineHeight: 1.25 }}>
            {shortStudy(p.setup.study)}
            <div style={{ fontSize: 11, color: MUTED }}>{studyDate(p.setup.study)}</div>
          </div>
        </Tooltip>
      ),
    },
    {
      key: "watchlist",
      title: kit.listWord.charAt(0).toUpperCase() + kit.listWord.slice(1),
      width: 130,
      filters: watchlists.map((w) => ({ text: w, value: w })),
      filteredValue: filters.watchlist ?? null,
      sorter: (a, b) => a.saved.setup.watchlist.localeCompare(b.saved.setup.watchlist),
      render: (_, { saved: p, config }) => (
        <Tooltip title={config.symbols.join(" ")}>{p.setup.watchlist || "—"} <span style={{ color: MUTED }}>({config.symbols.length})</span></Tooltip>
      ),
    },
    {
      key: "sector",
      title: <Tooltip title={`The sectors of its ${kit.listWord}, as a bot with it would get them: a sector holding at least a fifth of its symbols`}>Sectors</Tooltip>,
      width: 150,
      filters: sectors.map((s) => ({ text: s, value: s })),
      filteredValue: filters.sector ?? null,
      render: (_, { saved: p }) => <SectorTags sectors={p.sectors ?? []} />,
    },
    {
      key: "score",
      title: <Tooltip title="The weaker of its practice and exam returns: a setup is only as good as its worse window">Score</Tooltip>,
      width: 74,
      align: "right",
      sorter: by((r) => r.saved.setup.score),
      render: (_, { saved: p }) => <b style={{ color: color(p.setup.score) }}>{signed(p.setup.score)}</b>,
    },
    {
      key: "calm",
      title: <Tooltip title="Steadiness: its return per 1% of its worst drop, the weaker of practice and exam. Higher = more return for the bumps along the way">Ret/DD</Tooltip>,
      width: 70,
      align: "right",
      sorter: by((r) => calm(r.saved.setup.results)),
      render: (_, { saved: p }) => {
        const c = calm(p.setup.results);
        return c == null ? "—" : <span style={{ color: c >= 1 ? GREEN : c < 0 ? RED : undefined }}>{c.toFixed(2)}</span>;
      },
    },
    {
      key: "all",
      title: <Tooltip title="Its batch's full windows back to back: what it grew to. Batches with more windows chain more months; — where a batch has two windows or fewer">All</Tooltip>,
      width: 74,
      align: "right",
      sorter: by((r) => r.saved.setup.all),
      render: (_, { saved: p }) => <b style={{ color: color(p.setup.all) }}>{signed(p.setup.all, 0)}</b>,
    },
    ...windows.map((k) => windowColumns<C>(k)),
    ...(extras.length ? [extraColumn] : []),
    ...rules.map(({ r, values }) => ruleColumn(r, values, filters)),
    {
      key: "saved",
      title: "Saved",
      width: 96,
      sorter: (a, b) => a.saved.updated_at.localeCompare(b.saved.updated_at),
      defaultSortOrder: "descend",
      render: (_, { saved: p }) => <span style={{ color: MUTED, fontSize: 12 }}>{dayjs(p.updated_at).format("YYYY-MM-DD")}</span>,
    },
    {
      key: "actions",
      title: "",
      width: 104,
      fixed: pin ? "right" : undefined,
      render: (_, r) => (
        <Space size={2}>
          <Tooltip title={hint("Load into the backtest form")}>
            <Button size="small" type="text" icon={<ExperimentOutlined />} aria-label="Load into the backtest form" onClick={() => onLoad(r.config)} />
          </Tooltip>
          <Tooltip title={hint("Start a bot with this setup (opens the new bot form, filled in: pick the broker and check it first)")}>
            <Button size="small" type="text" icon={<RocketOutlined />} aria-label="Start a bot with this setup" onClick={() => startBot(r)} />
          </Tooltip>
          <Popconfirm title={`Delete the saved “${r.saved.name}”?`} description="Bots started from it keep their copy." okText="Delete"
            okButtonProps={{ danger: true }} onConfirm={() => remove(r.saved)}>
            <Tooltip title={hint("Delete this saved setup")}>
              <Button size="small" type="text" danger icon={<DeleteOutlined />} aria-label="Delete this saved setup" />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  if (error) return <Alert type="error" showIcon message={`Can't load the saved setups: ${error}`} />;
  if (!all) return <Spin />;
  if (!rows.length) {
    return (
      <Card>
        <Empty description={
          <span style={{ color: MUTED }}>
            Nothing saved from Test results yet. Save a test there (the save button on its row) and it shows up here, next to the ones saved from other batches.
          </span>
        } />
      </Card>
    );
  }

  return (
    <>
      <Typography.Paragraph style={{ color: MUTED, fontSize: 12, marginBottom: 8 }}>
        {rows.length} saved {rows.length === 1 ? "test" : "tests"} from {studies.length} {studies.length === 1 ? "batch" : "batches"}, each with the
        numbers it had when saved. A window's dates are its own batch's (hover a return): a 10-month exam isn't like for like with a 9-month one.
      </Typography.Paragraph>
      <Space wrap style={{ marginBottom: 8 }}>
        <Input.Search allowClear placeholder="Find a setup, code or symbol" value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: 240 }} />
        <span style={{ color: MUTED, fontSize: 12 }}>{shown.length} shown</span>
      </Space>
      <Table<Row<C>>
        size="small"
        rowKey={(r) => r.saved.id}
        dataSource={shown}
        columns={columns}
        scroll={{ x: "max-content" }}
        sticky
        pagination={shown.length > 50 ? { pageSize: 50, showSizeChanger: true } : false}
        onChange={(_, f) => setFilters(f)}
        expandable={{
          expandedRowRender: (r) => (
            <TestDetailView
              test={{ id: r.saved.setup.test_id, config: r.config, extra: r.saved.setup.extra, note: r.saved.setup.note }}
              periods={r.saved.setup.periods}
              kit={kit}
              gone="Its batch was replaced or deleted since this was saved: the charts went with it. The row's numbers are the copy kept when it was saved."
            />
          ),
        }}
      />
      {fromForms.length > 0 && (
        <Typography.Paragraph style={{ color: MUTED, fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          Not here: {fromForms.length} saved from a form, without test results ({fromForms.map((p) => `“${p.name}”`).join(", ")}). Only a setup
          saved from a test (its row on Test results, a bot's Tested setup) keeps them.
        </Typography.Paragraph>
      )}
    </>
  );
}
