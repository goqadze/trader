import { Form, InputNumber, DatePicker, Select, Button, Card, Switch } from "antd";
import dayjs, { Dayjs } from "dayjs";
import { useEffect } from "react";
import { CHECK_AT_OPTIONS, defaultRange } from "../checkAt";
import { DEFAULT_STRATEGY, STRATEGY_OPTIONS, strategyInfo, type StrategyId, type StrategyInfo } from "../strategies";
import type { DecideAt } from "../trading/types";
import type { RunConfig } from "../types";
import StrategyHelp from "./StrategyHelp";
import SymbolSelect from "./SymbolSelect";

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

  const submit = (v: FormValues) => {
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
          if (s) applySuggested(s.suggested);
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
        }}
      >
        <Form.Item name="strategy" label="Strategy">
          <Select options={STRATEGY_OPTIONS} popupMatchSelectWidth={false} listHeight={420} />
        </Form.Item>
        <StrategyHelp id={strategy} onApply={applySuggested} />
        <Form.Item name="symbol" label="Symbol" rules={[{ required: true }]}>
          <SymbolSelect />
        </Form.Item>
        <Form.Item name="range" label="Date range" rules={[{ required: true }]}>
          <DatePicker.RangePicker style={{ width: "100%" }} />
        </Form.Item>
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
        <Form.Item name="initial_cash" label="Initial cash ($)">
          <InputNumber min={100} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="min_confidence" label="Min confidence (0–1)">
          <InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="rebalance_days" label="Rebalance every N trading days">
          <InputNumber min={1} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="position_pct" label="Position size (fraction of cash)">
          <InputNumber min={0.1} max={1} step={0.1} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="stop_pct" label="Stop-loss (% below entry)">
          <InputNumber min={0.1} max={50} step={0.5} addonAfter="%" style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="target_pct" label="Take-profit (% above entry)">
          <InputNumber min={0.1} max={200} step={0.5} addonAfter="%" style={{ width: "100%" }} />
        </Form.Item>
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
