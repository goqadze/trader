import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Checkbox, Col, Form, InputNumber, Radio, Row, Select, Switch, Tooltip, Typography } from "antd";
import { DEFAULT_RULES, type DipRules, type DropMode, INTERVALS, type PriceTier, tierSpans } from "./types";

const MUTED = "#8b98b5";

/** The rules as the form holds them: percentages as numbers (5 = 5%), fractions on the wire. */
export type RulesForm = Omit<DipRules, "drop_pct" | "rise_pct" | "stop_pct" | "rebound_pct"> & {
  drop_pct: number; rise_pct: number; stop_pct: number; rebound_pct: number;
};

const PCT = ["drop_pct", "rise_pct", "stop_pct", "rebound_pct"] as const;

export function toRulesForm(r: DipRules): RulesForm {
  const out = { ...r } as RulesForm;
  for (const f of PCT) out[f] = +(r[f] * 100).toFixed(4);
  return out;
}

export function fromRulesForm(v: RulesForm): DipRules {
  const out = {
    interval: v.interval, drop_pct: v.drop_pct, drop_mode: v.drop_mode ?? DEFAULT_RULES.drop_mode,
    price_tiers: tidyTiers(v.price_tiers), drop_atr: v.drop_atr ?? DEFAULT_RULES.drop_atr,
    lookback: v.lookback, lookback_unit: v.interval === "1d" ? "days" : v.lookback_unit,
    drop_from: v.drop_from, target_mode: v.target_mode, rise_pct: v.rise_pct, stop_pct: v.stop_pct, max_positions: v.max_positions,
    reenable_days: v.reenable_days ?? 0, max_hold_days: v.max_hold_days ?? 0, news: !!v.news, trend_filter: !!v.trend_filter, rebound: v.rebound ?? DEFAULT_RULES.rebound,
    rebound_pct: v.rebound_pct, fractional: !!v.fractional,
  } as DipRules;
  // A value the form didn't send (an unmounted field) falls back to the default instead of 0, which the API refuses
  for (const f of PCT) out[f] = v[f] == null ? DEFAULT_RULES[f] : +(v[f] / 100).toFixed(6);
  return out;
}

/** The tiers as the API wants them: the last one without a top (it holds every price above). */
function tidyTiers(tiers?: PriceTier[] | null): PriceTier[] {
  const list = tiers?.length ? tiers : DEFAULT_RULES.price_tiers;
  return list.map((t, i) => ({ up_to: i === list.length - 1 ? null : t?.up_to ?? null, share: t?.share }));
}

// Short labels for the box (it is narrow in the backtest panel), the longer meaning in the list
const DROP_MODES: { value: DropMode; label: string; hint: string }[] = [
  { value: "percent", label: "all stocks", hint: "the same x for every stock" },
  { value: "price", label: "by price", hint: "smaller for pricier stocks (tiers)" },
  { value: "volatility", label: "by move", hint: "× each stock's usual daily move" },
];

/** A price tier's fall in % (x = 3% and 5/6 of it: 2.5), kept as its share of x so the tiers follow x when it
 *  changes. `x` in % as the form holds it. */
function TierFall({ value, onChange, x }: { value?: number; onChange?: (v: number | null) => void; x?: number }) {
  const ok = !!x && x > 0;
  return (
    <Tooltip title={value != null ? `${+(value * 100).toFixed(1)}% of x` : undefined}>
      <InputNumber
        value={ok && value != null ? +(value * x).toFixed(2) : null}
        onChange={(v) => onChange?.(ok && v != null ? v / x : null)}
        min={0.01}
        max={50}
        step={0.5}
        disabled={!ok}
        addonAfter="%"
        style={{ width: "100%" }}
      />
    </Tooltip>
  );
}

/** drop_mode "price": the tiers, each a top price and its share of x. Always mounted (hidden when unused): antd leaves
 *  an unmounted field out of the submitted values. */
function PriceTiers({ hidden }: { hidden: boolean }) {
  const form = Form.useFormInstance();
  const x: number | undefined = Form.useWatch("drop_pct", form);
  const tiers: PriceTier[] = Form.useWatch("price_tiers", form) ?? [];
  const spans = tierSpans(tiers.map((t) => ({ up_to: t?.up_to ?? null, share: t?.share ?? 1 })));
  return (
    <div style={hidden ? { display: "none" } : { marginBottom: 16 }}>
      <Typography.Text style={{ fontSize: 12, color: MUTED }}>
        By the price the fall is measured from: x for the first tier, a share of it for pricier stocks. Change x and every
        tier follows.
      </Typography.Text>
      <Form.List
        name="price_tiers"
        rules={[{
          validator: async (_, list: PriceTier[]) => {
            if (hidden) return;
            const tops = (list ?? []).slice(0, -1).map((t) => t?.up_to);
            if (tops.some((t, i) => t == null || (i > 0 && t <= (tops[i - 1] ?? 0)))) throw new Error("The top prices must go up");
          },
        }]}
      >
        {(fields, { add, remove }, { errors }) => (
          <>
            {fields.map((f, i) => {
              const last = i === fields.length - 1;
              return (
                <Row key={f.key} gutter={6} wrap={false} align="middle" style={{ marginTop: 6 }}>
                  <Col flex="1 1 50%">
                    {last ? (
                      <div style={{ padding: "4px 0 4px 11px", whiteSpace: "nowrap" }}>{spans[i] ?? "any price"}</div>
                    ) : (
                      <Tooltip title={spans[i]}>
                        <div>
                          <Form.Item name={[f.name, "up_to"]} noStyle rules={[{ required: !hidden, message: "A top price" }]}>
                            <InputNumber min={0.01} prefix="under $" style={{ width: "100%" }} />
                          </Form.Item>
                        </div>
                      </Tooltip>
                    )}
                  </Col>
                  <Col flex="1 1 50%">
                    <Form.Item name={[f.name, "share"]} noStyle rules={[{ required: !hidden, message: "A fall" }]}>
                      <TierFall x={x} />
                    </Form.Item>
                  </Col>
                  <Col flex="0 0 24px">
                    {!last && fields.length > 2 && <Button size="small" type="text" icon={<DeleteOutlined />} onClick={() => remove(f.name)} />}
                  </Col>
                </Row>
              );
            })}
            <Form.ErrorList errors={errors} />
            <Button
              size="small"
              type="dashed"
              icon={<PlusOutlined />}
              style={{ marginTop: 6 }}
              disabled={fields.length >= 8}
              onClick={() => {
                const prev = tiers[tiers.length - 2];
                add({ up_to: (prev?.up_to ?? 50) * 2, share: tiers[tiers.length - 1]?.share ?? 1 }, fields.length - 1);
              }}
            >
              Add a tier
            </Button>
          </>
        )}
      </Form.List>
    </div>
  );
}

/** The longest window per interval in trading days, for a live bot (its price feed keeps 60 days of 5- to 30-minute
 *  bars). A backtest reads years of bars, so it isn't limited. */
export const BOT_MAX_DAYS: Record<string, number> = { "5m": 40, "15m": 40, "30m": 40, "1h": 250, "1d": 250 };

const pct = (min: number, max: number, step: number) => <InputNumber min={min} max={max} step={step} addonAfter="%" style={{ width: "100%" }} />;

/** The dip buyer's rules, inside a Form whose values are a RulesForm. `forBot`: a live bot's limits on the window. */
export default function RulesFields({ forBot = false, compact = false }: { forBot?: boolean; compact?: boolean }) {
  const form = Form.useFormInstance();
  const interval = Form.useWatch("interval", form) ?? "15m";
  const unit = Form.useWatch("lookback_unit", form) ?? "days";
  const targetMode = Form.useWatch("target_mode", form) ?? "reference";
  const rebound = Form.useWatch("rebound", form) ?? true;
  const dropMode: DropMode = Form.useWatch("drop_mode", form) ?? "percent";
  const daily = interval === "1d";
  const span = compact ? 24 : 12;
  return (
    <>
      <Form.Item name="interval" label="Check" tooltip="At the end of each bar of this length from the 9:30 open (the last check is before the close), or once a day in the last 30 minutes. Exits are checked at the same times.">
        <Select options={INTERVALS.map((i) => ({ value: i.value, label: i.label }))} />
      </Form.Item>
      <Row gutter={12}>
        <Col span={span}>
          <Form.Item
            label="Buy after a fall of"
            required
            tooltip="x: how far under its reference price a symbol must be to buy it. For every stock: the same x. By price: x for the cheapest stocks, a share of it for pricier ones (the tiers below). By daily move: this many times the stock's usual daily move, so a calm stock needs a smaller fall and a wild one a bigger one."
          >
            <Row gutter={6} wrap={false}>
              {/* Both always mounted (one hidden): antd leaves an unmounted field out of the submitted values */}
              <Col flex="1 1 auto" style={dropMode === "volatility" ? { display: "none" } : undefined}>
                <Form.Item name="drop_pct" noStyle rules={[{ required: dropMode !== "volatility" }]}>
                  <InputNumber min={0.1} max={50} step={0.5} addonAfter="%" style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col flex="1 1 auto" style={dropMode === "volatility" ? undefined : { display: "none" }}>
                <Form.Item name="drop_atr" noStyle rules={[{ required: dropMode === "volatility" }]}>
                  <InputNumber min={0.1} max={10} step={0.1} addonAfter="× move" style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col flex="0 0 112px">
                <Form.Item name="drop_mode" noStyle>
                  <Select
                    options={DROP_MODES}
                    popupMatchSelectWidth={false}
                    optionRender={(o) => <span>{o.label} <span style={{ color: MUTED, fontSize: 12 }}>· {o.data.hint}</span></span>}
                  />
                </Form.Item>
              </Col>
            </Row>
          </Form.Item>
        </Col>
        <Col span={span}>
          <Form.Item label="During the last" required tooltip="y: the window the fall is measured in (trading days, or market hours)">
            <Row gutter={6} wrap={false}>
              <Col flex="1 1 auto">
                <Form.Item
                  name="lookback"
                  noStyle
                  dependencies={["interval", "lookback_unit"]}
                  rules={[
                    { required: true },
                    ({ getFieldValue }) => ({
                      validator: (_, n: number) => {
                        const iv = getFieldValue("interval");
                        const u = iv === "1d" ? "days" : getFieldValue("lookback_unit");
                        const minutes = INTERVALS.find((i) => i.value === iv)?.minutes ?? 15;
                        if (u === "hours" && n * 60 < minutes) return Promise.reject(new Error("Shorter than one check"));
                        const days = u === "days" ? n : n / 6.5;
                        if (forBot && days > BOT_MAX_DAYS[iv]) return Promise.reject(new Error(`At most ${BOT_MAX_DAYS[iv]} days with this check`));
                        return Promise.resolve();
                      },
                    }),
                  ]}
                >
                  <InputNumber min={1} max={1600} style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              <Col flex="0 0 96px">
                <Form.Item name="lookback_unit" noStyle>
                  <Select options={[{ value: "days", label: "days" }, { value: "hours", label: "hours", disabled: daily }]} />
                </Form.Item>
              </Col>
            </Row>
          </Form.Item>
        </Col>
      </Row>
      {daily && unit === "hours" && <Typography.Text type="warning" style={{ fontSize: 12 }}>A daily check measures in days.</Typography.Text>}
      <PriceTiers hidden={dropMode !== "price"} />
      {dropMode === "volatility" && (
        <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
          A stock's usual daily move is its average high-to-low range over the last 14 trading days (recounted every day).
          1.5 × a 2% daily move = a 3% fall; 1.5 × a 4% move = a 6% fall.
        </Typography.Paragraph>
      )}
      <Form.Item label="Then wait for the turn" tooltip="x1: after the fall, don't buy yet. Follow its lowest price and buy once it is back up this much from that low (bearish turned bullish), still under the price the fall started from. Off: buy as soon as it has fallen x%.">
        <Row gutter={8} wrap={false} align="middle">
          <Col flex="0 0 auto">
            <Form.Item name="rebound" valuePropName="checked" noStyle>
              <Switch checkedChildren="On" unCheckedChildren="Off" />
            </Form.Item>
          </Col>
          {/* Always mounted (only disabled): antd leaves an unmounted field out of the submitted values */}
          <Col flex="1 1 auto">
            <Form.Item name="rebound_pct" noStyle rules={[{ required: rebound }]}>
              <InputNumber min={0.1} max={50} step={0.25} addonBefore="up" addonAfter="% from the low" disabled={!rebound} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
      </Form.Item>
      <Form.Item name="drop_from" label="Measured from" tooltip="High: it rose, then turned down (the top of the window). Start: down x% compared with y days ago.">
        <Radio.Group
          options={[
            { value: "high", label: "the window's highest price" },
            { value: "start", label: "the price at its start" },
          ]}
        />
      </Form.Item>
      <Row gutter={12}>
        <Col span={targetMode === "percent" ? 16 : 24}>
          <Form.Item name="target_mode" label="Sell when" tooltip="Back at the price the fall was measured from (a 5% fall needs a 5.3% rise back), or a fixed rise above your buy">
            <Radio.Group
              options={[
                { value: "reference", label: "back at the starting price" },
                { value: "percent", label: "up by" },
              ]}
            />
          </Form.Item>
        </Col>
        {/* Always mounted, only hidden: antd leaves an unmounted field out of the submitted values, and the API
            needs rise_pct even when it isn't used */}
        <Col span={8} style={targetMode === "percent" ? undefined : { display: "none" }}>
          <Form.Item name="rise_pct" label="Rise" rules={[{ required: targetMode === "percent" }]}>
            {pct(0.5, 100, 0.5)}
          </Form.Item>
        </Col>
      </Row>
      <Row gutter={12}>
        <Col span={span}>
          <Form.Item name="stop_pct" label="Stop-loss" tooltip="z: sell when it falls this far under your buy, and blacklist the symbol (see Blacklist lasts)" rules={[{ required: true }]}>
            {pct(0.5, 50, 0.5)}
          </Form.Item>
        </Col>
        <Col span={span}>
          <Form.Item name="max_positions" label="Positions at once" tooltip="How many different symbols it may hold at the same time. The money is split into this many equal parts: with $10,000 and 5, each buy gets about $2,000. When more symbols fall than there are free places, the ones that fell the most are bought first; the others wait until something is sold.">
            <InputNumber min={1} max={20} style={{ width: "100%" }} />
          </Form.Item>
        </Col>
      </Row>
      <Form.Item
        name="reenable_days"
        label="Blacklist lasts"
        tooltip={forBot
          ? "After a stop-loss the bot doesn't buy that symbol for this many trading days, then watches it again (a stop on Monday with 2: buyable again on Wednesday). 0 = until you re-enable it yourself. You can always re-enable it sooner."
          : "After a stop-loss the symbol isn't bought for this many trading days. A live bot with the same setting does the same; 0 = for good (a live bot: until you re-enable it)."}
      >
        <InputNumber min={0} max={500} addonAfter="trading days (0 = until re-enabled)" style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item name="fractional" valuePropName="checked" style={{ marginBottom: 4 }}>
        <Switch checkedChildren="Fractional shares" unCheckedChildren="Whole shares" />
      </Form.Item>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        Fractional: buys part of a share when a part of the money is smaller than one share's price ($114 buys 0.38 of a
        $300 share). Alpaca allows it for most US stocks and ETFs, from $1; a symbol it can't split is bought in whole shares.
      </Typography.Paragraph>
      <Form.Item name="max_hold_days" label="Sell anyway after" tooltip="A fall that never comes back ties up a slot; 0 = hold until the target or the stop">
        <InputNumber min={0} max={250} addonAfter="trading days (0 = never)" style={{ width: "100%" }} />
      </Form.Item>
      <Form.Item name="news" valuePropName="checked" style={{ marginBottom: 4 }}>
        <Switch checkedChildren="News on" unCheckedChildren="News off" />
      </Form.Item>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        News on: before each buy it reads the latest headlines; bearish news means the fall may have a reason, so it doesn't buy
        that symbol for the rest of the day.
      </Typography.Paragraph>
      <Form.Item name="trend_filter" valuePropName="checked">
        <Checkbox>
          Only in an uptrend <span style={{ color: MUTED, fontSize: 12 }}>(50-day average above the 200-day: buy dips, not crashes)</span>
        </Checkbox>
      </Form.Item>
    </>
  );
}
