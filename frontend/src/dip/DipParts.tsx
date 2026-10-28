// The parts of a bot's page that differ for a dip buyer: what it holds (with each holding's target and stop), its
// watchlist (changed while it runs), its rules (the news switch right there), and its signals.

import { DeleteOutlined, ExperimentOutlined, PlusOutlined, StopOutlined, UndoOutlined } from "@ant-design/icons";
import {
  Alert, App as AntApp, Button, Card, Checkbox, Col, Descriptions, Form, Input, InputNumber, Modal, Pagination, Progress, Row, Select, Space,
  Switch, Table, Tag, Tooltip,
} from "antd";
import { useEffect, useRef, useState } from "react";
import { BOT_GROUPS, symbolOptions } from "../symbols";
import { tradingApi } from "../trading/api";
import { type Ask, AccountTag, useConfirm } from "../trading/confirm";
import { frac, localTime, nyTime, pct, pnlColor, usd } from "../trading/format";
import type { Bot, Holding } from "../trading/types";
import PresetPicker from "./PresetPicker";
import RulesFields, { type RulesForm, fromRulesForm, toRulesForm } from "./RulesFields";
import { SignalLine } from "./SignalBell";
import {
  DEFAULT_PRICE_TIERS, type DipBotUpdate, type DipPreset, type DipPresetConfig, type DipRules, type DipSignal, INTERVALS, type WatchItem,
  fallText, pickRules, sellText,
} from "./types";

const MUTED = "#8b98b5";

/** The bot's rules as DipRules (its columns are nullable: they're only set on dip bots). */
export const rulesOf = (b: Bot): DipRules => ({
  interval: b.interval ?? "15m", drop_pct: b.drop_pct ?? 0.05, drop_mode: b.drop_mode ?? "percent",
  price_tiers: b.price_tiers ?? DEFAULT_PRICE_TIERS, drop_atr: b.drop_atr ?? 1.5, lookback: b.lookback ?? 5, lookback_unit: b.lookback_unit ?? "days",
  drop_from: b.drop_from ?? "high", target_mode: b.target_mode ?? "reference", rise_pct: b.rise_pct ?? 0.05, stop_pct: b.stop_pct,
  max_positions: b.max_positions ?? 5, max_hold_days: b.max_hold_days ?? 0, news: !!b.news, trend_filter: !!b.trend_filter,
  rebound: !!b.rebound, rebound_pct: b.rebound_pct ?? 0.01, fractional: !!b.fractional, reenable_days: b.reenable_days ?? 0,
});

const intervalLabel = (b: Bot) => INTERVALS.find((i) => i.value === b.interval)?.label ?? b.interval;

/** "Buys a 5% fall under the 5-day high · every 15 minutes · stop 5%": a dip bot's rules in a few words. */
export const dipRules = (b: Bot) => `Buys ${fallText(rulesOf(b))} · ${intervalLabel(b)} · stop ${frac(b.stop_pct)}`;

/** Run an API call, show its error, then let the page refresh. */
function useAct(onChanged: () => void) {
  const { message } = AntApp.useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    try {
      await fn();
      if (ok) message.success(ok);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      onChanged();
    }
  };
  return { busy, act };
}

/** Ask before a change to this bot (see trading/confirm). */
function useBotConfirm(bot: Bot) {
  const ask = useConfirm();
  return (a: Omit<Ask, "bot">) => ask({ ...a, bot });
}

// ---------------------------------------------------------------------------
// Holdings
// ---------------------------------------------------------------------------

/** How far along from the stop to the target the price is: 0% at the stop, 100% at the target. */
function Progressbar({ h }: { h: Holding }) {
  if (h.stop_price == null || h.target_price == null || h.last_price == null) return null;
  const p = ((h.last_price - h.stop_price) / (h.target_price - h.stop_price)) * 100;
  return (
    <Tooltip title={`From the stop ${usd(h.stop_price)} (0%) to the target ${usd(h.target_price)} (100%)`}>
      <Progress percent={Math.max(0, Math.min(100, Math.round(p)))} size="small" showInfo={false} strokeColor={p >= 50 ? "#33c088" : "#d4a72c"} style={{ width: 90, margin: 0 }} />
    </Tooltip>
  );
}

export function DipHoldingsCard({ bot, marketOpen, onChanged }: { bot: Bot; marketOpen: boolean; onChanged: () => void }) {
  const { busy, act } = useAct(onChanged);
  const confirm = useBotConfirm(bot);
  const sell = (h: Holding) => confirm({
    title: `Sell all ${h.shares} ${h.symbol} at market now?`,
    content: "It stays on the watchlist (not blacklisted): it can be bought again from tomorrow.",
    okText: "Sell now",
    danger: true,
    onOk: () => act(`sell-${h.symbol}`, () => tradingApi.sellHolding(bot.id, h.symbol), "Sell order sent"),
  });
  const dist = (level: number | null, last: number | null) => (level != null && last ? pct(((level - last) / last) * 100, 1) : "—");
  return (
    <Card title="Holdings" size="small" style={{ height: "100%" }} extra={<span style={{ color: MUTED, fontSize: 12 }}>{bot.holdings.length} of {bot.max_positions} slots · cash {usd(bot.cash, 0)}</span>}>
      {bot.holdings.length === 0 ? (
        <Space direction="vertical" size={2}>
          <span>Holding cash only.</span>
          <span style={{ color: MUTED }}>
            {bot.status === "active" ? `It buys the next symbol that falls into the buy zone (${fallText(rulesOf(bot))}).` : "Paused: no buys until it's resumed."}
          </span>
        </Space>
      ) : (
        <Table<Holding>
          rowKey="symbol"
          size="small"
          pagination={false}
          scroll={{ x: 760 }}
          dataSource={bot.holdings}
          columns={[
            {
              title: "Symbol", dataIndex: "symbol",
              render: (s: string, h) => (
                <Space size={4}>
                  <b>{s}</b>
                  {!h.on_watchlist && <Tooltip title="Removed from the watchlist: held until it exits"><Tag style={{ margin: 0 }}>off list</Tag></Tooltip>}
                </Space>
              ),
            },
            { title: "Shares", dataIndex: "shares", align: "right" },
            { title: "Bought", dataIndex: "entry_price", align: "right", render: (v: number | null, h) => <Tooltip title={`Opened ${nyTime(h.opened_at)}`}>{usd(v)}</Tooltip> },
            { title: "Last", dataIndex: "last_price", align: "right", render: (v: number | null) => usd(v) },
            {
              title: "P&L", dataIndex: "unrealized_pnl", align: "right",
              render: (v: number, h) => <span style={{ color: pnlColor(v) }}>{usd(v)} <span style={{ fontSize: 12 }}>({pct((v / h.cost_basis) * 100, 1)})</span></span>,
            },
            {
              title: "Target", dataIndex: "target_price", align: "right",
              render: (v: number | null, h) => <span><span style={{ color: "#33c088" }}>{usd(v)}</span> <span style={{ color: MUTED, fontSize: 12 }}>{dist(v, h.last_price)}</span></span>,
            },
            {
              title: "Stop", dataIndex: "stop_price", align: "right",
              render: (v: number | null, h) => <span><span style={{ color: "#ef5b6b" }}>{usd(v)}</span> <span style={{ color: MUTED, fontSize: 12 }}>{dist(v, h.last_price)}</span></span>,
            },
            { title: "", key: "progress", render: (_, h) => <Progressbar h={h} /> },
            {
              title: "", key: "sell",
              render: (_, h) => (
                <Tooltip title={marketOpen ? "" : "Orders only during market hours"}>
                  <Button size="small" danger disabled={!marketOpen} loading={busy === `sell-${h.symbol}`} onClick={() => sell(h)}>Sell</Button>
                </Tooltip>
              ),
            },
          ]}
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Watchlist
// ---------------------------------------------------------------------------

function statusTag(w: WatchItem, bot: Bot) {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  if (w.status === "blacklisted") {
    const until = w.reenable_on ? `until ${new Date(`${w.reenable_on}T12:00:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })}` : "until you re-enable it";
    return <Tooltip title={`${w.blacklist_reason ?? ""} (${localTime(w.blacklisted_at)}), ${until}`}><Tag color="red">blacklisted</Tag></Tooltip>;
  }
  if (w.held) return <Tag color="blue">held</Tag>;
  if (w.news_blocked_on === today) return <Tooltip title="Bearish news: not bought today"><Tag color="gold">news: not today</Tag></Tooltip>;
  if (bot.rebound && w.dip_reference != null) {
    return (
      <Tooltip title={`Fell from ${usd(w.dip_reference)}; lowest since ${usd(w.trough_price)}${w.trough_at ? ` (${nyTime(w.trough_at)})` : ""}. Bought once it's back up ${frac(bot.rebound_pct ?? 0.01)} from that low: ${usd(w.rebound_at)} or more.`}>
        <Tag color={w.turned_at ? "cyan" : "geekblue"}>{w.turned_at ? "turned up" : "waiting for the turn"}</Tag>
      </Tooltip>
    );
  }
  if (w.in_zone && bot.trend_filter && w.trend_ok === false) return <Tooltip title="In the buy zone, but not in an uptrend"><Tag color="orange">downtrend</Tag></Tooltip>;
  if (w.in_zone) return <Tag color="geekblue">buy zone</Tag>;
  return <Tag>watching</Tag>;
}

export function WatchlistCard({ bot, onChanged }: { bot: Bot; onChanged: () => void }) {
  const { busy, act } = useAct(onChanged);
  const confirm = useBotConfirm(bot);
  const [adding, setAdding] = useState<string[]>([]);
  const stillHeld = (w: WatchItem) => (w.held ? " It's held: the position stays and still sells at its target or stop." : "");
  const blacklist = (w: WatchItem) => confirm({
    title: `Blacklist ${w.symbol}?`,
    content: `No more buys of ${w.symbol} until you re-enable it.${stillHeld(w)}`,
    okText: "Blacklist",
    danger: true,
    onOk: () => act(`bl-${w.symbol}`, () => tradingApi.blacklistSymbol(bot.id, w.symbol), `${w.symbol} blacklisted`),
  });
  const reenable = (w: WatchItem) => confirm({
    title: `Re-enable ${w.symbol}?`,
    content: `The bot may buy ${w.symbol} again from the next check.`,
    okText: "Re-enable",
    onOk: () => act(`en-${w.symbol}`, () => tradingApi.enableSymbol(bot.id, w.symbol), `${w.symbol} re-enabled`),
  });
  const remove = (w: WatchItem) => confirm({
    title: `Stop watching ${w.symbol}?`,
    content: `It comes off the watchlist and won't be bought again (you can add it back).${stillHeld(w)}`,
    okText: "Remove",
    danger: true,
    onOk: () => act(`rm-${w.symbol}`, () => tradingApi.removeSymbol(bot.id, w.symbol), `${w.symbol} removed`),
  });
  const add = () => act("add", async () => {
    await tradingApi.addSymbols(bot.id, adding);
    setAdding([]);
  }, `Added ${adding.join(", ")}`);
  const blacklisted = bot.watchlist.filter((w) => w.status === "blacklisted").length;
  return (
    <Card
      title={<span>Watchlist <span style={{ color: MUTED, fontWeight: 400, fontSize: 12 }}>{bot.watchlist.length} symbols{blacklisted ? `, ${blacklisted} blacklisted` : ""} · as of the last check</span></span>}
      size="small"
      style={{ marginTop: 16 }}
      extra={
        bot.status !== "archived" && (
          <Space.Compact>
            <Select
              mode="tags"
              size="small"
              value={adding}
              onChange={(v: string[]) => setAdding([...new Set(v.map((s) => s.trim().toUpperCase()).filter(Boolean))])}
              options={symbolOptions((s) => `${s.symbol} · ${s.name}`, BOT_GROUPS)}
              optionLabelProp="value"
              tokenSeparators={[",", " "]}
              placeholder="Add symbols"
              style={{ minWidth: 220 }}
            />
            <Button size="small" type="primary" icon={<PlusOutlined />} disabled={!adding.length} loading={busy === "add"} onClick={add}>Add</Button>
          </Space.Compact>
        )
      }
    >
      <Table<WatchItem>
        rowKey="symbol"
        size="small"
        pagination={false}
        scroll={{ x: 760 }}
        dataSource={bot.watchlist}
        locale={{ emptyText: "Nothing on the watchlist: add a few symbols." }}
        columns={[
          { title: "Symbol", dataIndex: "symbol", render: (s: string) => <b>{s}</b> },
          { title: "Price", dataIndex: "last_price", align: "right", render: (v: number | null) => usd(v) },
          {
            title: <Tooltip title={`The ${bot.lookback}-${bot.lookback_unit === "hours" ? "hour" : "day"} ${bot.drop_from === "start" ? "start" : "high"} the fall is measured from`}>Reference</Tooltip>,
            dataIndex: "reference_price", align: "right", render: (v: number | null) => usd(v),
          },
          {
            title: "From it", dataIndex: "drop", align: "right", sorter: (a, b) => (b.drop ?? -1) - (a.drop ?? -1),
            render: (d: number | null, w) => (d == null ? <Tooltip title="Not enough prices yet for the window"><span style={{ color: MUTED }}>—</span></Tooltip>
              : <span style={{ color: d >= (w.buy_drop ?? bot.drop_pct ?? 1) ? "#4f8cff" : d > 0 ? undefined : "#33c088" }}>{pct(-d * 100, 1)}</span>),
          },
          {
            title: <Tooltip title={bot.rebound ? "In the buy zone at or under the first price; a fall being followed is bought at or above the second (its low + the turn)" : "In the buy zone at or under this price"}>Buy at</Tooltip>,
            dataIndex: "buy_below", align: "right",
            render: (v: number | null, w) => (w.rebound_at != null
              ? <Tooltip title="Waiting for the turn: bought at or above this price"><span style={{ color: "#13c2c2" }}>≥ {usd(w.rebound_at)}</span></Tooltip>
              : bot.drop_mode === "price" || bot.drop_mode === "volatility"
                ? <Tooltip title={w.buy_drop == null ? "Its usual daily move isn't known yet: no buy zone" : `A ${frac(w.buy_drop)} fall ${bot.drop_mode === "price" ? "at its price" : "for its daily move"}`}>
                    {v == null || w.buy_drop == null ? <span style={{ color: MUTED }}>—</span> : <span>{usd(v)} <span style={{ color: MUTED, fontSize: 12 }}>({frac(w.buy_drop)})</span></span>}
                  </Tooltip>
                : usd(v)),
          },
          { title: "Status", key: "status", render: (_, w) => statusTag(w, bot) },
          {
            title: "News", dataIndex: "news_sentiment",
            render: (s: string | null, w) => (s ? <Tooltip title={`Asked ${localTime(w.news_at)}`}><Tag color={s === "bearish" ? "red" : s === "bullish" ? "green" : "default"}>{s}</Tag></Tooltip> : <span style={{ color: MUTED }}>—</span>),
          },
          { title: "Checked", dataIndex: "checked_at", render: (v: string | null) => <span style={{ color: MUTED, fontSize: 12 }}>{v ? nyTime(v) : "not yet"}</span> },
          {
            title: "", key: "actions", align: "right",
            render: (_, w) => bot.status === "archived" ? null : (
              <Space size={4}>
                {w.status === "blacklisted" ? (
                  <Tooltip title="Re-enable: it may be bought again from the next check">
                    <Button size="small" icon={<UndoOutlined />} loading={busy === `en-${w.symbol}`} onClick={() => reenable(w)}>
                      Re-enable
                    </Button>
                  </Tooltip>
                ) : (
                  <Tooltip title="Blacklist: no more buys until you re-enable it">
                    <Button size="small" icon={<StopOutlined />} loading={busy === `bl-${w.symbol}`} onClick={() => blacklist(w)} aria-label={`Blacklist ${w.symbol}`} />
                  </Tooltip>
                )}
                <Tooltip title="Remove from the watchlist">
                  <Button size="small" icon={<DeleteOutlined />} loading={busy === `rm-${w.symbol}`} onClick={() => remove(w)} aria-label={`Remove ${w.symbol}`} />
                </Tooltip>
              </Space>
            ),
          },
        ]}
      />
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------

export function DipParamsCard({ bot, onEdit, onChanged }: { bot: Bot; onEdit: () => void; onChanged: () => void }) {
  const { busy, act } = useAct(onChanged);
  const confirm = useBotConfirm(bot);
  const r = rulesOf(bot);
  /** A switch asks first; the change goes through on OK (until then the switch stays as it was). */
  const flip = (key: "rebound" | "fractional" | "news", on: boolean, title: string, content: string, done: string) => confirm({
    title,
    content: `From the next check: ${content}`,
    okText: on ? "Turn on" : "Turn off",
    onOk: () => act(key, () => tradingApi.updateDip(bot.id, { [key]: on }), done),
  });
  const rebound = (on: boolean) => (on
    ? flip("rebound", on, "Wait for the turn?", `a symbol in the buy zone is bought only once it's back up ${frac(r.rebound_pct)} from its low, not as soon as it has fallen.`,
      `Waiting for a ${frac(r.rebound_pct)} turn up before buying`)
    : flip("rebound", on, "Stop waiting for the turn?", `it buys as soon as a symbol is in the buy zone (${fallText({ ...r, rebound: false })}), without waiting for it to turn up.`,
      "Buying as soon as it has fallen"));
  const fractional = (on: boolean) => (on
    ? flip("fractional", on, "Buy fractions of a share?", "when a slot is smaller than one share's price, it buys part of a share.", "Buying fractions of a share")
    : flip("fractional", on, "Whole shares only?", "it buys whole shares only: a slot smaller than one share's price buys nothing. Fractions it holds now still sell as usual.",
      "Buying whole shares only"));
  const news = (on: boolean) => (on
    ? flip("news", on, "Turn the news check on?", "before each buy it reads the symbol's news; bearish news blocks buying it for the rest of the day.",
      "News on: bearish news now blocks a buy")
    : flip("news", on, "Turn the news check off?", "it buys on prices only, without reading the news.", "News off: prices only"));
  return (
    <Card title="Rules" size="small" style={{ height: "100%" }} extra={<Button size="small" type="link" onClick={onEdit} disabled={bot.status === "archived"}>Edit</Button>}>
      <Descriptions column={1} size="small">
        <Descriptions.Item label="Buys">{fallText(r)}</Descriptions.Item>
        <Descriptions.Item label="Wait for the turn">
          <Space>
            <Switch size="small" checked={r.rebound} loading={busy === "rebound"} disabled={bot.status === "archived"} onChange={rebound} />
            <span style={{ color: MUTED, fontSize: 12 }}>{r.rebound ? `buys once it's up ${frac(r.rebound_pct)} from its low` : "off: buys as soon as it has fallen"}</span>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="Sells">{sellText(r)}{r.max_hold_days ? `, or after ${r.max_hold_days} trading days` : ""}</Descriptions.Item>
        <Descriptions.Item label="Stop-loss">
          {frac(r.stop_pct)} under the buy, then blacklisted {r.reenable_days ? `for ${r.reenable_days} trading day${r.reenable_days > 1 ? "s" : ""}` : "until you re-enable it"}
        </Descriptions.Item>
        <Descriptions.Item label="Checks">{intervalLabel(bot)}</Descriptions.Item>
        <Descriptions.Item label="Positions">up to {r.max_positions} at once, {frac(1 / r.max_positions)} of the equity each</Descriptions.Item>
        <Descriptions.Item label="Fractional shares">
          <Space>
            <Switch size="small" checked={r.fractional} loading={busy === "fractional"} disabled={bot.status === "archived"} onChange={fractional} />
            <span style={{ color: MUTED, fontSize: 12 }}>{r.fractional ? "buys part of a share when a slot is smaller than its price" : "off: whole shares only"}</span>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="News">
          <Space>
            <Switch size="small" checked={r.news} loading={busy === "news"} disabled={bot.status === "archived"} onChange={news} />
            <span style={{ color: MUTED, fontSize: 12 }}>{r.news ? "bearish news blocks a buy for the day" : "off: prices only"}</span>
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="Trend filter">{r.trend_filter ? "only dips in an uptrend (50-day avg > 200-day)" : "off"}</Descriptions.Item>
        <Descriptions.Item label="Breaker · slippage · fee">
          {bot.max_drawdown_pct ? `${frac(bot.max_drawdown_pct)} drawdown` : "off"} · {frac(bot.slippage_pct)} · {frac(bot.fee_pct)}
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}

interface EditValues extends RulesForm {
  name: string;
  slippage_pct: number;
  fee_pct: number;
  max_drawdown_pct: number;
}

/** Change a dip bot's rules while it runs (from the next check on; held positions keep their target and stop). A saved
 *  setup fills in its rules, and can switch the bot to its watchlist and to the tested setup it was saved from too. */
export function DipEditForm({ open, bot, onCancel, onSaved }: { open: boolean; bot: Bot; onCancel: () => void; onSaved: () => void }) {
  const [form] = Form.useForm<EditValues>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();
  const [switchTo, setSwitchTo] = useState<DipPreset | null>(null); // the saved setup picked
  const [takeList, setTakeList] = useState(true);
  const [takeTest, setTakeTest] = useState(true);
  // Load values only when the modal opens: the page re-polls the bot every 15s (see BotForm)
  const botRef = useRef(bot);
  botRef.current = bot;
  useEffect(() => {
    if (open) {
      const b = botRef.current;
      form.setFieldsValue({
        ...toRulesForm(rulesOf(b)),
        name: b.name,
        slippage_pct: +(b.slippage_pct * 100).toFixed(4),
        fee_pct: +(b.fee_pct * 100).toFixed(4),
        max_drawdown_pct: +(b.max_drawdown_pct * 100).toFixed(4),
      });
      setError(null);
      setSwitchTo(null);
    }
  }, [open, form]);

  // What else the picked saved setup would switch: the watchlist (a held symbol left out still exits at its target or
  // stop), and the tested setup it was saved from (the Tested setup popup's results)
  const watching = (bot.watchlist ?? []).map((w) => w.symbol);
  const target = switchTo?.config.symbols ?? [];
  const added = target.filter((s) => !watching.includes(s));
  const removed = watching.filter((s) => !target.includes(s));
  const stillHeld = removed.filter((s) => bot.holdings.some((h) => h.symbol === s));
  const listDiffers = added.length > 0 || removed.length > 0;
  const listChange = [added.length > 0 && `adds ${added.join(" ")}`, removed.length > 0 && `leaves out ${removed.join(" ")}`]
    .filter(Boolean).join(" · ") + (stillHeld.length ? ` (held ${stillHeld.join(" ")}: still sold at the target or stop)` : "");
  const test = switchTo?.setup;
  const testDiffers = !!test && (bot.setup?.test_id !== test.test_id || bot.setup?.code !== test.code);
  const newList = !!switchTo && listDiffers && takeList;
  const newTest = !!switchTo && testDiffers && takeTest;
  // A saved setup from a form has no tested setup to switch to: the bot keeps its own, say so
  const keepsTest = !!switchTo && !test ? bot.setup : null;
  const switchBox = !!switchTo && (listDiffers || testDiffers || !!keepsTest);

  const submit = async () => {
    const v = await form.validateFields();
    const body: DipBotUpdate = {
      ...fromRulesForm(v),
      name: v.name.trim(),
      slippage_pct: +(v.slippage_pct / 100).toFixed(6),
      fee_pct: +(v.fee_pct / 100).toFixed(6),
      max_drawdown_pct: +(v.max_drawdown_pct / 100).toFixed(6),
      ...(newList && { symbols: target }),
      ...(newTest && switchTo && { setup_preset_id: switchTo.id }),
    };
    const run = async () => {
      setSaving(true);
      setError(null);
      try {
        await tradingApi.updateDip(bot.id, body);
        onSaved();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSaving(false);
      }
    };
    if (!bot.live || !(newList || newTest) || !switchTo) return run();
    confirm({
      title: `Switch this real-money bot to “${switchTo.name}”?`,
      content: (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          <li>The rules in the form, from the next check (what it holds keeps the target and stop it was bought with)</li>
          {newList && <li>Watchlist: {listChange}</li>}
          {newTest && test && <li>Tested setup: {test.name.startsWith(test.code) ? test.name : `${test.code} ${test.name}`}</li>}
        </ul>
      ),
      okText: "Switch",
      danger: true,
      bot,
      onOk: run,
    });
  };

  const pctInput = (max: number, step: number) => <InputNumber min={0} max={max} step={step} addonAfter="%" style={{ width: "100%" }} />;
  const load = (c: DipPresetConfig, saved: DipPreset) => {
    form.setFieldsValue(toRulesForm(pickRules(c)));
    void form.validateFields(["lookback"]).catch(() => undefined); // a backtest's window may be too long for a bot: say so now
    setSwitchTo(saved);
    setTakeList(true);
    setTakeTest(true);
  };
  return (
    <Modal title="Edit dip buyer" open={open} onCancel={onCancel} onOk={submit} okText="Save" okButtonProps={{ loading: saving }} width={640} destroyOnClose>
      <Form<EditValues> form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item>
        <Form.Item
          label="From a saved setup"
          tooltip="Fills in its rules. Where its watchlist or its tested setup differ from the bot's, you can switch those too (ticked below)"
          style={switchBox ? { marginBottom: 8 } : undefined}
        >
          <PresetPicker onLoad={load} onClear={() => setSwitchTo(null)} />
        </Form.Item>
        {switchTo && switchBox && (
          <div style={{ border: "1px solid #303030", borderRadius: 6, padding: "8px 12px", marginBottom: 16 }}>
            {(listDiffers || testDiffers) && (
              <div style={{ color: MUTED, fontSize: 12, marginBottom: 4 }}>Switch to “{switchTo.name}” besides its rules:</div>
            )}
            {listDiffers && (
              <Checkbox checked={takeList} onChange={(e) => setTakeList(e.target.checked)} style={{ display: "flex", marginBottom: 4 }}>
                Its watchlist ({target.length} symbol{target.length === 1 ? "" : "s"})
                <div style={{ color: MUTED, fontSize: 12 }}>{listChange}</div>
              </Checkbox>
            )}
            {testDiffers && test && (
              <Checkbox checked={takeTest} onChange={(e) => setTakeTest(e.target.checked)} style={{ display: "flex" }}>
                Its tested setup <Tag icon={<ExperimentOutlined />} color="geekblue" style={{ marginInlineEnd: 0 }}>{test.code}</Tag>
                <div style={{ color: MUTED, fontSize: 12 }}>
                  The Tested setup popup shows its results{bot.setup ? ` instead of ${bot.setup.code}'s` : ""}
                </div>
              </Checkbox>
            )}
            {keepsTest && (
              <div style={{ color: MUTED, fontSize: 12, marginTop: listDiffers ? 4 : 0 }}>
                “{switchTo.name}” has no tested setup (saved from a form), so the bot stays linked to{" "}
                <Tag icon={<ExperimentOutlined />} color="geekblue" style={{ marginInlineEnd: 0 }}>{keepsTest.code}</Tag>
                {" "}and its results
              </div>
            )}
          </div>
        )}
        <RulesFields forBot />
        <Row gutter={12}>
          <Col span={8}><Form.Item name="max_drawdown_pct" label="Drawdown breaker">{pctInput(100, 5)}</Form.Item></Col>
          <Col span={8}><Form.Item name="slippage_pct" label="Slippage buffer">{pctInput(5, 0.01)}</Form.Item></Col>
          <Col span={8}><Form.Item name="fee_pct" label="Fee (paper only)">{pctInput(5, 0.01)}</Form.Item></Col>
        </Row>
        <Alert type="info" showIcon message="New rules apply from the next check. A position already held keeps the target and stop it was bought with." />
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Signals
// ---------------------------------------------------------------------------

const PAGE_SIZES = [20, 50, 100];

/** Signals, newest first: a page at a time in a box that scrolls. Given `bots` (the Dip buyer page's feed of all of them,
 *  archived ones included for their names), each line says whose signal it is, live ones in red, and one bot can be picked. */
export function SignalsList({ signals, bots }: { signals: DipSignal[]; bots?: Bot[] }) {
  const [kind, setKind] = useState<string>("all");
  const [botId, setBotId] = useState<number | "all">("all");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(PAGE_SIZES[0]);
  const box = useRef<HTMLDivElement>(null);
  const byId = new Map((bots ?? []).map((b) => [b.id, b]));
  const picks = (bots ?? []).filter((b) => signals.some((s) => s.bot_id === b.id));
  const shown = signals.filter((s) => (kind === "all" || s.kind === kind) && (botId === "all" || s.bot_id === botId));
  const current = Math.min(page, Math.max(1, Math.ceil(shown.length / size))); // a filter or a refresh left fewer pages
  const turn = (p: number, n: number) => {
    setPage(n === size ? p : 1);
    setSize(n);
    box.current?.scrollTo({ top: 0 });
  };
  return (
    <>
      <Space style={{ marginBottom: 8 }} wrap>
        <Select
          size="small"
          value={kind}
          onChange={(v) => { setKind(v); setPage(1); }}
          style={{ width: 180 }}
          options={[
            { value: "all", label: "All signals" },
            { value: "down", label: "⬇ Buy zone" },
            { value: "rebound", label: "↗ Turned up" },
            { value: "up", label: "⬆ Back up" },
            { value: "stop", label: "⛔ Stop-loss" },
            { value: "time", label: "⏱ Held too long" },
            { value: "news", label: "📰 Bearish news" },
          ]}
        />
        {picks.length > 1 && (
          <Select
            size="small"
            value={botId}
            onChange={(v) => { setBotId(v); setPage(1); }}
            style={{ width: 220 }}
            options={[
              { value: "all" as const, label: "All bots" },
              ...picks.map((b) => ({ value: b.id, label: <span>{b.name} <AccountTag bot={b} /></span> })),
            ]}
          />
        )}
        <span style={{ color: MUTED, fontSize: 12 }}>For your information, whatever the bot did. Turn on sound or read-aloud with the bell at the top.</span>
      </Space>
      {shown.length === 0 ? (
        <div style={{ color: MUTED, padding: 8 }}>No signals yet. One appears when a symbol falls into the buy zone or gets back up.</div>
      ) : (
        <>
          <div ref={box} style={{ maxHeight: "70vh", overflowY: "auto", paddingRight: 4 }}>
            {shown.slice((current - 1) * size, current * size).map((s) => (
              <SignalLine key={s.id} s={s} bot={byId.get(s.bot_id)} />
            ))}
          </div>
          <Pagination
            size="small"
            align="end"
            style={{ marginTop: 8 }}
            current={current}
            pageSize={size}
            total={shown.length}
            onChange={turn}
            showSizeChanger
            pageSizeOptions={PAGE_SIZES}
            hideOnSinglePage={shown.length <= PAGE_SIZES[0]}
            showTotal={(total, [from, to]) => `${from}–${to} of ${total}`}
          />
        </>
      )}
    </>
  );
}
