import { Button, Card, Checkbox, DatePicker, Form, Input, InputNumber, Segmented, Select, Space, Typography } from "antd";
import { Dayjs } from "dayjs";
import { CHECK_AT_OPTIONS, clampToIntraday, defaultRange, earliestIntradayStart, needsIntraday } from "../checkAt";
import { STRATEGIES, strategyInfo, strategyShortName, type StrategyId } from "../strategies";
import type { DecideAt } from "../trading/types";
import type { RunConfig } from "../types";

const MUTED = "#8b98b5";
const STYLES = ["trend", "momentum", "breakout", "mean reversion", "event"] as const;

// antd form values (dates are dayjs objects; percents are sent as fractions).
interface FormValues {
  strategies: StrategyId[];
  settings: "suggested" | "same";
  symbol: string;
  range: [Dayjs, Dayjs];
  initial_cash: number;
  min_confidence: number;
  position_pct: number;
  rebalance_days: number;
  stop_pct: number;
  target_pct: number;
  decide_at: DecideAt | "suggested";
}

// "Each strategy's suggested time" first: it's the default, and how each strategy is meant to trade
const COMPARE_CHECK_AT = [{ value: "suggested", label: "Each strategy's suggested time" }, ...CHECK_AT_OPTIONS];

/** Can this window replay 10:00 decisions? (30-minute prices only go back 60 days.) */
const intradayOk = (range: [Dayjs, Dayjs] | undefined) => !range?.[0] || !range[0].isBefore(earliestIntradayStart(), "day");

/** The check time a strategy runs with. "suggested" follows the strategy (Gap and go after the open, News
 *  catalyst at both), falling back to the close when the window is older than the 30-minute prices. */
function checkAt(id: StrategyId, v: Partial<FormValues>): DecideAt {
  if (v.decide_at && v.decide_at !== "suggested") return v.decide_at;
  const suggested = strategyInfo(id)!.suggested.decide_at ?? "close";
  return intradayOk(v.range) ? suggested : "close";
}

/** One RunConfig per picked strategy. "suggested" gives each its own rebalance/stop/target (a breakout checked
 *  weekly would miss most breakouts); "same" runs them all on identical settings. */
function toConfigs(v: FormValues): RunConfig[] {
  return v.strategies.map((id) => {
    const g = strategyInfo(id)!.suggested;
    const own = v.settings === "suggested";
    return {
      symbol: v.symbol.trim().toUpperCase(),
      start: v.range[0].format("YYYY-MM-DD"),
      end: v.range[1].format("YYYY-MM-DD"),
      initial_cash: v.initial_cash,
      min_confidence: v.min_confidence,
      position_pct: v.position_pct,
      rebalance_days: own ? g.rebalance_days : v.rebalance_days,
      stop_pct: own ? g.stop_pct : v.stop_pct / 100,
      target_pct: own ? g.target_pct : v.target_pct / 100,
      strategy: id,
      decide_at: checkAt(id, v),
    };
  });
}

/** Rough count of decision-service calls (one per decision day), so a big comparison doesn't surprise. */
function decisionCalls(v: Partial<FormValues>): number | null {
  if (!v.range?.[0] || !v.range?.[1] || !v.strategies?.length) return null;
  let weekdays = 0;
  for (let d = v.range[0]; !d.isAfter(v.range[1], "day"); d = d.add(1, "day")) if (d.day() % 6 !== 0) weekdays++;
  return v.strategies.reduce((n, id) => {
    const every = v.settings === "same" ? v.rebalance_days || 1 : strategyInfo(id)!.suggested.rebalance_days;
    return n + Math.ceil(weekdays / every) * (checkAt(id, v) === "both" ? 2 : 1);
  }, 0);
}

interface Props {
  onRun: (configs: RunConfig[]) => void;
  running: boolean;
}

/** Left-hand panel of the comparison: which strategies, on what, with which settings. */
export default function CompareForm({ onRun, running }: Props) {
  const [form] = Form.useForm<FormValues>();
  const values = Form.useWatch([], form) as Partial<FormValues> | undefined;
  const settings = values?.settings ?? "suggested";
  const calls = values ? decisionCalls(values) : null;
  const decideAt = values?.decide_at ?? "suggested";
  const limitDates = decideAt !== "suggested" && needsIntraday(decideAt);
  // Suggested times on a window too old for 30-minute prices: these strategies lose their after-the-open check
  const fallBack = decideAt === "suggested" && !intradayOk(values?.range)
    ? (values?.strategies ?? []).filter((id) => needsIntraday(strategyInfo(id)?.suggested.decide_at))
    : [];

  return (
    <Card title="Compare strategies" size="small">
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={(v) => onRun(toConfigs(v))}
        onValuesChange={(changed: Partial<FormValues>) => {
          // Checking after the open needs 30-minute prices: move a window that starts too early to the first day with them
          if (changed.decide_at && changed.decide_at !== "suggested" && needsIntraday(changed.decide_at)) {
            form.setFieldValue("range", clampToIntraday(form.getFieldValue("range")));
          }
        }}
        initialValues={{
          strategies: STRATEGIES.map((s) => s.id),
          settings: "suggested",
          symbol: "AAPL",
          range: defaultRange(),
          initial_cash: 10000,
          min_confidence: 0.6,
          position_pct: 1.0,
          rebalance_days: 5,
          stop_pct: 4,
          target_pct: 8,
          decide_at: "suggested",
        }}
      >
        <Form.Item
          label={
            <Space size={4}>
              Strategies
              <Button type="link" size="small" onClick={() => form.setFieldValue("strategies", STRATEGIES.map((s) => s.id))}>
                all
              </Button>
              <Button type="link" size="small" onClick={() => form.setFieldValue("strategies", [])}>
                none
              </Button>
            </Space>
          }
        >
          <Form.Item name="strategies" noStyle rules={[{ type: "array", min: 2, message: "Pick at least 2 to compare" }]}>
            <Checkbox.Group style={{ display: "block" }}>
              {STYLES.map((style) => (
                <div key={style} style={{ marginBottom: 6 }}>
                  <div style={{ fontSize: 11, color: MUTED, textTransform: "uppercase", letterSpacing: 0.5 }}>{style}</div>
                  {STRATEGIES.filter((s) => s.style === style).map((s) => (
                    <div key={s.id}>
                      <Checkbox value={s.id} title={s.summary}>
                        {s.name}
                      </Checkbox>
                    </div>
                  ))}
                </div>
              ))}
            </Checkbox.Group>
          </Form.Item>
        </Form.Item>
        <Form.Item name="symbol" label="Symbol" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item
          name="range"
          label="Date range"
          rules={[{ required: true }]}
          extra={
            fallBack.length > 0 && (
              <span style={{ color: "#faad14" }}>
                {fallBack.map(strategyShortName).join(" and ")} normally check after the open, which needs 30-minute prices (they go
                back to {earliestIntradayStart().format("YYYY-MM-DD")}). With this window they check before the close.
              </span>
            )
          }
        >
          <DatePicker.RangePicker style={{ width: "100%" }} disabledDate={(d) => limitDates && d.isBefore(earliestIntradayStart(), "day")} />
        </Form.Item>
        <Form.Item
          name="decide_at"
          label="Check at (New York time)"
          tooltip="When each decision day decides, like a trading bot's “Check at”. Suggested: Gap and go after the open (10:00), News catalyst at both times, the rest before the close (15:30). Pick one time to run every strategy on it."
          extra={limitDates ? `10:00 is replayed from 30-minute prices, which go back to ${earliestIntradayStart().format("YYYY-MM-DD")}.` : undefined}
        >
          <Select options={COMPARE_CHECK_AT} />
        </Form.Item>
        <Form.Item name="initial_cash" label="Initial cash ($)">
          <InputNumber min={100} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="min_confidence" label="Min confidence (0–1)">
          <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="position_pct" label="Position size (fraction of cash)">
          <InputNumber min={0.1} max={1} step={0.1} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item
          name="settings"
          label="Rebalance, stop-loss and take-profit"
          extra={
            settings === "suggested"
              ? "Each strategy runs on its own suggested values (see the Trading Strategies guide)."
              : "Every strategy runs on the values below: a like-for-like test of the rules alone."
          }
        >
          <Segmented block options={[{ label: "Suggested per strategy", value: "suggested" }, { label: "Same for all", value: "same" }]} />
        </Form.Item>
        {settings === "same" && (
          <>
            <Form.Item name="rebalance_days" label="Rebalance every N trading days">
              <InputNumber min={1} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="stop_pct" label="Stop-loss (% below entry)">
              <InputNumber min={0.1} max={50} step={0.5} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="target_pct" label="Take-profit (% above entry)">
              <InputNumber min={0.1} max={200} step={0.5} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
          </>
        )}
        <Button type="primary" htmlType="submit" block loading={running}>
          Run {values?.strategies?.length ?? 0} backtests
        </Button>
        {calls != null && (
          <Typography.Text style={{ display: "block", marginTop: 8, fontSize: 12, color: MUTED }}>
            About {calls.toLocaleString()} decisions, each a decision-service call. A few strategies run at a time; the
            rest wait their turn.
          </Typography.Text>
        )}
      </Form>
    </Card>
  );
}
