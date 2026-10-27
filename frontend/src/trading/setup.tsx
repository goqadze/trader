// The tested setup a bot was started from (Test results' "Start a bot"): a button on the bot table and the bot's page
// that opens its name, study, results per window and whether the bot's rules still match it. trading-service copies the
// test into the bot when it is made (main._setup_snapshot), so this stays when the study is replaced. A red LIVE on the
// button of a bot that isn't live: real money trades the same setup (BotOut.setup_live). Save keeps the setup with its
// results as a saved setup (POST /bots/{id}/setup/save), to start a bot from it later or switch a running dip bot to it.

import { CheckCircleOutlined, ExperimentOutlined, SaveOutlined } from "@ant-design/icons";
import { App as AntApp, Alert, Button, Descriptions, Input, type InputRef, Modal, Space, Table, Tag, Tooltip, Typography } from "antd";
import dayjs from "dayjs";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { PERIOD_LABEL } from "../studies/kit";
import type { SavedSetup } from "../studies/types";
import { tradingApi } from "./api";
import { isDip, isRotation } from "./format";
import type { Bot, BotSetup, SetupNumbers } from "./types";

const MUTED = "#8b98b5";
const GREEN = "#33c088";
const RED = "#ef5b6b";
const color = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? undefined : v > 0 ? GREEN : RED);
const signed = (v: number | null | undefined, digits = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`);
const share = (v: unknown) => `${+(Number(v) * 100).toFixed(2)}%`;
const yesNo = (v: unknown) => (v ? "on" : "off");
const day = (d: string) => dayjs(d).format("MMM D, YYYY");
// "All": the windows it chains (the grid's: all but the bear market) and their months, for its tooltip
const allTip = (setup: BotSetup) => {
  const full = Object.entries(setup.periods).filter(([k]) => k !== "bear");
  const months = full.reduce((n, [, [a, b]]) => n + Math.round(dayjs(b).diff(dayjs(a), "day") / 30.44), 0);
  return `All ${full.length} windows back to back (${months} months): what it grew to`;
};

/** A tested setup's Score or All in the bot table: "+50.2%" in color, "—" for a bot not started from a test. */
export function SetupPct({ v, digits = 1 }: { v: number | null | undefined; digits?: number }) {
  return v == null ? <span style={{ color: MUTED }}>—</span> : <span style={{ color: color(v) }}>{signed(v, digits)}</span>;
}

/** The live (real-money) bots trading a bot's setup, as links ("#224, #221"); a click closes the popup it is in. */
function LiveBots({ ids, onClick }: { ids: number[]; onClick: () => void }) {
  return <>{ids.map((id, i) => <span key={id}>{i > 0 && ", "}<Link to={`/trading/${id}`} onClick={onClick}>#{id}</Link></span>)}</>;
}

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
  // A dip bot's watchlist as it is now (a switch to a saved setup, or a symbol added by hand, changes it); a rotation's universe
  const start = isDip(bot) && bot.watchlist?.length ? bot.watchlist.map((w) => w.symbol) : bot.universe ?? [];
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

function SetupDetails({ bot, setup, close }: { bot: Bot; setup: BotSetup; close: () => void }) {
  const diffs = setupDifferences(bot, setup);
  const live = bot.setup_live ?? [];
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
        {setup.all != null && (
          <Descriptions.Item label="All">
            <Tooltip title={allTip(setup)}><span style={{ color: color(setup.all) }}>{signed(setup.all, 0)}</span></Tooltip>
          </Descriptions.Item>
        )}
        <Descriptions.Item label={setup.switched ? "Bot switched to it" : "Bot started from it"}>{day(setup.started_at)}</Descriptions.Item>
        {live.length > 0 && (
          <Descriptions.Item label={<span style={{ color: RED }}>{bot.live ? "Other live bots on it" : "Live bots on it"}</span>}>
            <Tooltip title="Bots trading real money (Alpaca live) that were started from this tested setup">
              <span><LiveBots ids={live} onClick={close} /> <Tag color="red" style={{ marginInlineStart: 4 }}>REAL MONEY</Tag></span>
            </Tooltip>
          </Descriptions.Item>
        )}
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

/** The popup's footer: Save (the setup under a name you pick, its test's name to start with, as a saved setup of its kind
 *  with its results) and Close. Says where it is saved already: a saved setup from the same test. */
function SetupFooter({ bot, setup, close }: { bot: Bot; setup: BotSetup; close: () => void }) {
  const { message } = AntApp.useApp();
  const [saved, setSaved] = useState<SavedSetup<unknown>[] | null>(null); // its kind's saved setups
  const [name, setName] = useState<string | null>(null); // the name dialog is open
  const [busy, setBusy] = useState(false);
  const nameRef = useRef<InputRef>(null);
  const rotation = setup.strategy === "rotation";

  useEffect(() => {
    (rotation ? tradingApi.rotationPresets() : tradingApi.dipPresets()).then(setSaved, () => setSaved([]));
  }, [rotation]);

  const same = saved?.find((p) => p.setup?.test_id === setup.test_id && p.setup?.code === setup.code);
  const typed = name?.trim() ?? "";
  const clash = saved?.find((p) => p.name.toLowerCase() === typed.toLowerCase());

  const save = async () => {
    if (!typed) return;
    setBusy(true);
    try {
      const p = await tradingApi.saveBotSetup(bot.id, typed);
      setSaved((list) => [...(list ?? []).filter((x) => x.id !== p.id), p]);
      setName(null);
      message.success(`Saved as “${p.name}”: pick it under saved setups in a new bot${rotation ? "" : " or a dip bot's Edit"}`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    // On a phone the note takes its own line and the buttons stay together under it
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
      {same && (
        <Typography.Text style={{ color: MUTED, fontSize: 12, flex: "1 1 200px", textAlign: "left" }}>
          <CheckCircleOutlined style={{ color: GREEN }} /> Saved as “{same.name}”
        </Typography.Text>
      )}
      <Space>
        <Tooltip title={`Keep this setup and its results under a name: start a new bot from it later${rotation ? "" : ", or switch a running dip bot to it (Edit on the bot's page)"}`}>
          <Button icon={<SaveOutlined />} onClick={() => setName(same?.name ?? setup.name)} disabled={saved === null}>Save</Button>
        </Tooltip>
        <Button onClick={close}>Close</Button>
      </Space>
      <Modal
        title="Save this tested setup"
        open={name !== null}
        onCancel={() => setName(null)}
        onOk={save}
        okText={clash ? "Replace" : "Save"}
        okButtonProps={{ loading: busy, disabled: !typed }}
        afterOpenChange={(open) => open && nameRef.current?.focus({ cursor: "all" })}
        width={420}
        destroyOnClose
      >
        <Input ref={nameRef} value={name ?? ""} onChange={(e) => setName(e.target.value)} onPressEnter={save} maxLength={60} />
        <Typography.Text style={{ display: "block", fontSize: 12, color: clash ? "#d4a72c" : MUTED, marginTop: 8 }}>
          {clash ? `Replaces the saved “${clash.name}”. ` : ""}
          Keeps its rules, {rotation ? "universe" : "watchlist"}, capital and test results. Pick it later in a new bot's saved setups
          {rotation ? "" : ", or in a running dip bot's Edit to switch that bot to it"}.
        </Typography.Text>
      </Modal>
    </div>
  );
}

/** "Tested setup": opens the setup a bot was started from. Nothing for a bot that wasn't. compact: the bot table's. */
export function SetupButton({ bot, compact = false }: { bot: Bot; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const setup = bot.setup;
  if (!setup) return null;
  const live = bot.live ? [] : bot.setup_live ?? []; // a live bot is real money itself: no mark
  const tag = (
    <Tag
      icon={<ExperimentOutlined />}
      color="geekblue"
      role="button"
      aria-label={`Tested setup ${setup.name}${live.length ? ", used by live bots" : ""}`}
      style={{
        cursor: "pointer", marginInlineEnd: 0, ...(live.length > 0 && { borderColor: RED }),
        ...(compact && { fontSize: 11, lineHeight: "18px", padding: "0 5px" }),
      }}
      onClick={() => setOpen(true)}
    >
      {compact ? setup.code : `Tested setup ${setup.code}`}
      {live.length > 0 && (
        <b style={{
          display: "inline-block", marginInlineStart: 5, padding: "0 4px", borderRadius: 3, background: RED, color: "#fff",
          fontSize: compact ? 10 : 11, lineHeight: compact ? "14px" : "16px", letterSpacing: 0.4,
        }}>
          LIVE
        </b>
      )}
    </Tag>
  );
  return (
    // The popup is drawn outside the table, but its clicks (X, Close, the dark mask) still bubble up React's tree to the
    // bot table's row, which opens the bot's page: stop them here
    <span onClick={(e) => e.stopPropagation()}>
      {/* Hidden while the popup is open: after a tap on a phone it would stay up over the popup */}
      {live.length ? (
        <Tooltip open={open ? false : undefined} title={`Real money trades this setup too: live bot${live.length > 1 ? "s" : ""} ${live.map((i) => `#${i}`).join(", ")}`}>{tag}</Tooltip>
      ) : tag}
      <Modal
        title={<><ExperimentOutlined /> {setup.name}</>}
        open={open}
        onCancel={() => setOpen(false)}
        footer={<SetupFooter bot={bot} setup={setup} close={() => setOpen(false)} />}
        width={760}
        destroyOnClose
      >
        <SetupDetails bot={bot} setup={setup} close={() => setOpen(false)} />
      </Modal>
    </span>
  );
}
