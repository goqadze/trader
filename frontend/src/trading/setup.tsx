// The tested setup a bot was started from (Test results' "Start a bot"): a button on the bot table and the bot's page
// that opens its name, study, results per window and whether the bot's rules still match it. trading-service copies the
// test into the bot when it is made (main._setup_snapshot), so this stays when the study is replaced.

import { ExperimentOutlined } from "@ant-design/icons";
import { Alert, Button, Descriptions, Modal, Table, Tag, Tooltip, Typography } from "antd";
import dayjs from "dayjs";
import { useState } from "react";
import { PERIOD_LABEL } from "../studies/kit";
import { isRotation } from "./format";
import type { Bot, BotSetup, SetupNumbers } from "./types";

const MUTED = "#8b98b5";
const GREEN = "#33c088";
const RED = "#ef5b6b";
const color = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? undefined : v > 0 ? GREEN : RED);
const signed = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);
const share = (v: unknown) => `${+(Number(v) * 100).toFixed(2)}%`;
const yesNo = (v: unknown) => (v ? "on" : "off");
const day = (d: string) => dayjs(d).format("MMM D, YYYY");

/** The new bot form's note that the bot will keep this tested setup, and a way to drop the link. */
export function FromSetupNote({ name, onUnlink }: { name: string; onUnlink?: () => void }) {
  return (
    <Alert
      type="info"
      showIcon
      icon={<ExperimentOutlined />}
      style={{ marginBottom: 12 }}
      message={<>From the tested setup <b>{name}</b></>}
      description="The bot keeps its name and test results: Tested setup on the bot's page and in the bot list. Changing the rules here is fine; the popup then lists what differs."
      action={onUnlink && <Button size="small" type="text" onClick={onUnlink}>Don't link</Button>}
    />
  );
}

type Rule = [key: string, label: string, text: (v: unknown) => string];
// The rules a test and a bot share (same names as the backtest), in words. Symbols and capital are compared apart
const DIP_RULES: Rule[] = [
  ["interval", "Checks every", String], ["drop_mode", "Fall measured", String], ["drop_pct", "Fall", share],
  ["drop_atr", "Fall (× daily move)", (v) => `${v}×`], ["drop_from", "Fall from", String], ["lookback", "Window", String],
  ["lookback_unit", "Window unit", String], ["target_mode", "Sells", (v) => (v === "reference" ? "back at the high" : "at a % gain")],
  ["rise_pct", "Take profit", (v) => `+${share(v)}`], ["stop_pct", "Stop", share], ["max_positions", "Slots", String],
  ["rebound", "Waits for a turn", yesNo], ["rebound_pct", "Turn", share], ["reenable_days", "Blacklist days", String],
  ["max_hold_days", "Sell after days", String], ["trend_filter", "Uptrend only", yesNo], ["news", "News check", yesNo],
  ["fractional", "Fractions", yesNo],
];
const ROTATION_RULES: Rule[] = [
  ["top_n", "Holds", String], ["lookback_months", "Momentum months", String], ["skip_months", "Skips months", String],
  ["abs_filter", "Only what rose", yesNo], ["fractional", "Fractions", yesNo],
];
// Rules only the dip setups in that mode use: a difference in the others doesn't change what the bot does
const ONLY_WHEN: Record<string, (c: Record<string, unknown>) => boolean> = {
  drop_atr: (c) => c.drop_mode === "volatility",
  rise_pct: (c) => c.target_mode !== "reference",
  rebound_pct: (c) => !!c.rebound,
};

const same = (a: unknown, b: unknown) =>
  typeof a === "number" && typeof b === "number" ? Math.abs(a - b) < 1e-9 : JSON.stringify(a) === JSON.stringify(b);

/** What the bot does differently from the test: "Stop: 12% → 10%", symbols added or removed, another capital. */
export function setupDifferences(bot: Bot, setup: BotSetup): string[] {
  const c = setup.config;
  const b = bot as unknown as Record<string, unknown>;
  const out: string[] = [];
  for (const [key, label, text] of isRotation(bot) ? ROTATION_RULES : DIP_RULES) {
    const tested = c[key], now = b[key];
    if (tested == null || now == null || same(tested, now)) continue;
    if (ONLY_WHEN[key] && !ONLY_WHEN[key](c) && !ONLY_WHEN[key](b)) continue;
    out.push(`${label}: ${text(tested)} → ${text(now)}`);
  }
  const testedSymbols = (c.symbols as string[] | undefined) ?? [];
  const start = bot.universe ?? [];
  const added = start.filter((s) => !testedSymbols.includes(s));
  const removed = testedSymbols.filter((s) => !start.includes(s));
  if (added.length) out.push(`Symbols added: ${added.join(" ")}`);
  if (removed.length) out.push(`Symbols left out: ${removed.join(" ")}`);
  const cash = c.initial_cash as number | null | undefined;
  if (cash && !same(cash, bot.allocated_cash)) out.push(`Capital: $${cash.toLocaleString()} → $${bot.allocated_cash.toLocaleString()}`);
  return out;
}

interface Row extends SetupNumbers {
  key: string;
  dates?: [string, string];
}

function SetupDetails({ bot, setup }: { bot: Bot; setup: BotSetup }) {
  const diffs = setupDifferences(bot, setup);
  const rows: Row[] = Object.keys({ ...setup.periods, ...setup.results }).map((k) => ({ key: k, dates: setup.periods[k], ...setup.results[k] }));
  const symbols = (setup.config.symbols as string[] | undefined) ?? [];
  return (
    <>
      <Descriptions size="small" column={{ xs: 1, sm: 2 }} style={{ marginBottom: 12 }}>
        <Descriptions.Item label="Study">{setup.study}</Descriptions.Item>
        <Descriptions.Item label="Test">{setup.code}{setup.pick != null && <Tag color="gold" style={{ marginLeft: 6 }}>pick {setup.pick}</Tag>}</Descriptions.Item>
        <Descriptions.Item label="Watchlist">
          <Tooltip title={symbols.join(" ")}>{setup.watchlist || "—"} <span style={{ color: MUTED }}>({symbols.length})</span></Tooltip>
        </Descriptions.Item>
        <Descriptions.Item label="Score">
          <Tooltip title="The weaker of its practice and exam returns"><span style={{ color: color(setup.score) }}>{signed(setup.score)}</span></Tooltip>
        </Descriptions.Item>
        <Descriptions.Item label="Bot started from it">{day(setup.started_at)}</Descriptions.Item>
      </Descriptions>
      <Table<Row>
        size="small"
        pagination={false}
        rowKey="key"
        dataSource={rows}
        scroll={{ x: 600 }}
        columns={[
          {
            title: "Window", key: "window", fixed: "left", width: 120,
            render: (_, r) => <><b>{PERIOD_LABEL[r.key] ?? r.key}</b>{r.dates && <div style={{ color: MUTED, fontSize: 11 }}>{day(r.dates[0])} – {day(r.dates[1])}</div>}</>,
          },
          { title: "Return", key: "ret", align: "right", render: (_, r) => <span style={{ color: color(r.total_return_pct), fontWeight: 600 }}>{signed(r.total_return_pct)}</span> },
          { title: <Tooltip title="Holding its watchlist in equal parts over the same window">Buy & hold</Tooltip>, key: "hold", align: "right", render: (_, r) => <span style={{ color: color(r.buy_hold_return_pct) }}>{signed(r.buy_hold_return_pct)}</span> },
          { title: <Tooltip title="Its biggest fall from a high, in the window">Worst drop</Tooltip>, key: "dd", align: "right", render: (_, r) => signed(r.max_drawdown_pct) },
          { title: "Trades", key: "trades", align: "right", render: (_, r) => r.num_trades ?? "—" },
          { title: "Wins", key: "wins", align: "right", render: (_, r) => (r.win_rate_pct == null ? "—" : `${r.win_rate_pct.toFixed(0)}%`) },
        ]}
      />
      {setup.extra && (
        <Alert type="warning" showIcon style={{ marginTop: 12 }} message={`Tested with a filter the bot doesn't have: ${setup.extra}`} />
      )}
      <Alert
        style={{ marginTop: 12 }}
        type={diffs.length ? "warning" : "success"}
        showIcon
        message={diffs.length ? "The bot's rules differ from the tested setup, so these results fit it less" : "The bot has the tested rules, watchlist and capital"}
        description={diffs.length ? <ul style={{ margin: 0, paddingLeft: 18 }}>{diffs.map((d) => <li key={d}>{d}</li>)}</ul> : undefined}
      />
      {setup.note && <Typography.Paragraph style={{ color: MUTED, fontSize: 12, marginTop: 12, marginBottom: 0 }}>{setup.note}</Typography.Paragraph>}
      <Typography.Paragraph style={{ color: MUTED, fontSize: 12, marginTop: 8, marginBottom: 0 }}>
        A past test, not a forecast: the bot trades today's prices.
      </Typography.Paragraph>
    </>
  );
}

/** "Tested setup": opens the setup a bot was started from. Nothing for a bot that wasn't. compact: the bot table's. */
export function SetupButton({ bot, compact = false }: { bot: Bot; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const setup = bot.setup;
  if (!setup) return null;
  return (
    // The popup is drawn outside the table, but its clicks (X, Close, the dark mask) still bubble up React's tree to the
    // bot table's row, which opens the bot's page: stop them here
    <span onClick={(e) => e.stopPropagation()}>
      <Tag
        icon={<ExperimentOutlined />}
        color="geekblue"
        role="button"
        aria-label={`Tested setup ${setup.name}`}
        style={{ cursor: "pointer", marginInlineEnd: 0, ...(compact && { fontSize: 11, lineHeight: "18px", padding: "0 5px" }) }}
        onClick={() => setOpen(true)}
      >
        {compact ? setup.code : `Tested setup ${setup.code}`}
      </Tag>
      <Modal
        title={<><ExperimentOutlined /> {setup.name}</>}
        open={open}
        onCancel={() => setOpen(false)}
        footer={<Button onClick={() => setOpen(false)}>Close</Button>}
        width={760}
        destroyOnClose
      >
        <SetupDetails bot={bot} setup={setup} />
      </Modal>
    </span>
  );
}
