import { Form, InputNumber, DatePicker, Select, Button, Card, Switch } from "antd";
import dayjs, { Dayjs } from "dayjs";
import { useEffect } from "react";
import { CHECK_AT_OPTIONS, defaultRange } from "../checkAt";
import {
  DEFAULT_STRATEGY, ENTRY_OPTIONS, HTF_OPTIONS, INTRADAY_DEFAULTS, STRATEGY_OPTIONS, hasEntryChoice, isIntraday, strategyInfo, type IntradaySettings,
  type StrategyId, type StrategyInfo,
} from "../strategies";
import type { DecideAt } from "../trading/types";
import type { Entry, Htf, RunConfig } from "../types";
import StrategyHelp from "./StrategyHelp";
import SymbolSelect from "./SymbolSelect";
import { isFuture } from "../symbols";

// antd form values (dates are dayjs objects; we format them on submit).
interface FormValues {
  symbol: string;
  range: [Dayjs, Dayjs];
  initial_cash: number;
  min_confidence: number;
  rebalance_days: number;
  position_pct: number;
  stop_pct: number; // percent in the form; sent as a fraction
  target_pct: number;
  strategy: StrategyId;
  decide_at: DecideAt;
  max_drawdown_pct: number; // percent in the form
  news: boolean;
  // Intraday strategies only (percents in the form)
  risk_pct: number;
  sides: "long" | "both";
  slippage_pct: number;
  entry: Entry;
  htf: Htf;
}

interface Props {
  onRun: (cfg: RunConfig) => void;
  running: boolean;
  prefill?: RunConfig; // fill the form with this run's settings (e.g. "Re-check with news" from a scan)
}

/** A RunConfig back into the form's units (dates as dayjs, fractions as percents). */
function toFormValues(c: RunConfig): Partial<FormValues> {
  const pct = (v: number | undefined) => (v == null ? undefined : +(v * 100).toFixed(2));
  return {
    symbol: c.symbol,
    range: [dayjs(c.start), dayjs(c.end)],
    initial_cash: c.initial_cash,
    min_confidence: c.min_confidence,
    rebalance_days: c.rebalance_days,
    position_pct: c.position_pct,
    stop_pct: pct(c.stop_pct),
    target_pct: pct(c.target_pct),
    strategy: c.strategy,
    decide_at: c.decide_at ?? "close",
    max_drawdown_pct: pct(c.max_drawdown_pct),
    news: c.news ?? true,
    ...(c.risk_pct != null && { risk_pct: pct(c.risk_pct) }),
    ...(c.sides && { sides: c.sides }),
    ...(c.slippage_pct != null && { slippage_pct: pct(c.slippage_pct) }),
    ...(c.entry && { entry: c.entry }),
    ...(c.htf && { htf: c.htf }),
  };
}

// Left-hand configuration panel. On submit it maps the antd form values to the
// RunConfig shape backtest-service expects and calls onRun.
export default function ConfigForm({ onRun, running, prefill }: Props) {
  const [form] = Form.useForm<FormValues>();
  useEffect(() => {
    if (prefill) form.setFieldsValue(toFormValues(prefill));
  }, [prefill, form]);
  const strategy = Form.useWatch("strategy", form);
  // The form shows percents; the catalog stores fractions
  const applySuggested = (g: StrategyInfo["suggested"]) =>
    form.setFieldsValue({
      rebalance_days: g.rebalance_days,
      stop_pct: +(g.stop_pct * 100).toFixed(2),
      target_pct: +(g.target_pct * 100).toFixed(2),
      decide_at: g.decide_at ?? "close",
    });
  const applyIntraday = (g: IntradaySettings) =>
    form.setFieldsValue({ risk_pct: +(g.risk_pct * 100).toFixed(2), sides: g.sides, slippage_pct: +(g.slippage_pct * 100).toFixed(3) });
  const intraday = isIntraday(strategy);

  // Every field's value, also the ones hidden for this kind of strategy (onFinish would only pass the visible ones)
  const submit = () => {
    const v = form.getFieldsValue(true) as FormValues;
    onRun({
      symbol: v.symbol.trim().toUpperCase(),
      start: v.range[0].format("YYYY-MM-DD"),
      end: v.range[1].format("YYYY-MM-DD"),
      initial_cash: v.initial_cash,
      min_confidence: v.min_confidence,
      rebalance_days: v.rebalance_days,
      position_pct: v.position_pct,
      stop_pct: v.stop_pct / 100,
      target_pct: v.target_pct / 100,
      strategy: v.strategy,
      decide_at: v.decide_at,
      max_drawdown_pct: v.max_drawdown_pct / 100,
      news: v.news,
      ...(isIntraday(v.strategy) && { risk_pct: v.risk_pct / 100, sides: v.sides, slippage_pct: v.slippage_pct / 100 }),
      ...(hasEntryChoice(v.strategy) && { entry: v.entry, htf: v.htf }),
    });
  };

  return (
    <Card title="Configuration" size="small">
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={submit}
        // Picking a strategy loads its suggested rebalance, stop and take-profit; edit them afterwards if you like
        onValuesChange={(changed: Partial<FormValues>) => {
          const s = changed.strategy && strategyInfo(changed.strategy);
          if (s?.intraday) applyIntraday(s.intraday);
          else if (s) applySuggested(s.suggested);
        }}
        initialValues={{
          symbol: "AAPL",
          // The last year up to today. Today's bar only counts once the market has closed (backtest-service drops it before)
          range: defaultRange(),
          initial_cash: 10000,
          min_confidence: 0.6,
          rebalance_days: 5,
          position_pct: 1.0,
          stop_pct: 4, // same defaults as decision-service (STOP_PCT / TARGET_PCT)
          target_pct: 8,
          strategy: DEFAULT_STRATEGY,
          decide_at: "close",
          max_drawdown_pct: 20, // the bots' default
          news: true,
          risk_pct: INTRADAY_DEFAULTS.risk_pct * 100,
          sides: INTRADAY_DEFAULTS.sides,
          slippage_pct: INTRADAY_DEFAULTS.slippage_pct * 100,
          entry: "limit",
          htf: "off",
        }}
      >
        <Form.Item name="strategy" label="Strategy">
          <Select options={STRATEGY_OPTIONS} popupMatchSelectWidth={false} listHeight={420} />
        </Form.Item>
        <StrategyHelp id={strategy} onApply={applySuggested} onApplyIntraday={applyIntraday} />
        <Form.Item
          name="symbol"
          label="Symbol"
          dependencies={["strategy"]}
          rules={[
            { required: true },
            ({ getFieldValue }) => ({
              validator: (_, v?: string) => (isFuture(v) && !isIntraday(getFieldValue("strategy"))
                ? Promise.reject(new Error("Futures run with the intraday strategies only"))
                : Promise.resolve()),
            }),
          ]}
        >
          <SymbolSelect />
        </Form.Item>
        <Form.Item name="range" label="Date range" rules={[{ required: true }]}>
          <DatePicker.RangePicker style={{ width: "100%" }} />
        </Form.Item>
        {intraday && (
          <>
            <Form.Item name="risk_pct" label="Risk per trade" tooltip="What one trade may lose, as a share of the account: the distance from the entry to the setup's stop decides how many shares (or contracts). Shares are capped by the cash (no leverage); futures by their margin, 10% of each contract's value.">
              <InputNumber min={0.1} max={5} step={0.25} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
            {hasEntryChoice(strategy) && (
              <Form.Item
                name="entry"
                label="Entry"
                tooltip="Limit: wait for the price to come back to the fair value gap's middle (a better price, but often it never comes back and the order is cancelled at 11:00). At market: in at the next 5-minute bar's open as soon as the setup completes (every setup trades, at a worse price, so each win pays less). Same setup, stop and target either way."
              >
                <Select options={ENTRY_OPTIONS} popupMatchSelectWidth={false} />
              </Form.Item>
            )}
            {hasEntryChoice(strategy) && (
              <Form.Item
                name="htf"
                label="Higher-timeframe trend"
                tooltip="Like an ICT trader reading the bigger picture first: only take longs while that timeframe trends up and shorts while it trends down (up = its last finished candle closed above the average of its last 20). Built from regular hours. It only removes setups, so expect fewer trades."
              >
                <Select options={HTF_OPTIONS} />
              </Form.Item>
            )}
            <Form.Item name="sides" label="Trade" tooltip="Long and short takes the bearish setups too (live, shorting needs a margin account).">
              <Select options={[{ value: "both", label: "Long and short" }, { value: "long", label: "Long only" }]} />
            </Form.Item>
            <Form.Item name="slippage_pct" label="Slippage per market fill" tooltip="How much worse than the price a market order fills (entries at the open, stops, the 15:55 exit). QQQ and SPY trade with a one-cent spread (~0.002%); with tight intraday stops this number decides a lot, so try 0 and 0.05% too. Limit fills (entries at a price, targets) have none. Futures ignore it: a tick per market fill and a fee per contract.">
              <InputNumber min={0} max={1} step={0.005} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
          </>
        )}
        {!intraday && (
          <>
            <Form.Item name="decide_at" label="Check at (New York time)" tooltip="When a decision day decides, exactly like a trading bot's “Check at”: the day as it stood at 15:30 or 10:00, filled at that moment's price, then the stop and target guard the rest of the day. Both does the two.">
              <Select options={CHECK_AT_OPTIONS} />
            </Form.Item>
            <Form.Item
              name="news"
              label="News"
              valuePropName="checked"
              tooltip="On: each decision reads the news (RAG + an LLM sentiment) like a live bot. Off: technical only, decided on the price alone: much faster and no OpenAI cost, but News catalyst has nothing to trade on."
            >
              <Switch checkedChildren="on" unCheckedChildren="technical only" />
            </Form.Item>
          </>
        )}
        <Form.Item name="initial_cash" label="Initial cash ($)">
          <InputNumber min={100} style={{ width: "100%" }} />
        </Form.Item>
        {!intraday && (
          <>
            <Form.Item name="min_confidence" label="Min confidence (0–1)">
              <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="rebalance_days" label="Rebalance every N trading days">
              <InputNumber min={1} style={{ width: "100%" }} />
            </Form.Item>
          </>
        )}
        <Form.Item name="position_pct" label="Position size (fraction of cash)">
          <InputNumber min={0.1} max={1} step={0.1} style={{ width: "100%" }} />
        </Form.Item>
        {!intraday && (
          <>
            <Form.Item name="stop_pct" label="Stop-loss (% below entry)">
              <InputNumber min={0.1} max={50} step={0.5} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="target_pct" label="Take-profit (% above entry)">
              <InputNumber min={0.1} max={200} step={0.5} addonAfter="%" style={{ width: "100%" }} />
            </Form.Item>
          </>
        )}
        <Form.Item name="max_drawdown_pct" label="Drawdown breaker" tooltip="Like a trading bot: once equity falls this far below its peak, stop deciding (the stop-loss and target still guard an open position). 0 = off.">
          <InputNumber min={0} max={99} step={5} addonAfter="%" style={{ width: "100%" }} />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={running}>
          Run backtest
        </Button>
      </Form>
    </Card>
  );
}
