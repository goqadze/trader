import { Form, Input, InputNumber, DatePicker, Select, Button, Card } from "antd";
import dayjs, { Dayjs } from "dayjs";
import { DEFAULT_STRATEGY, STRATEGY_OPTIONS, type StrategyId } from "../strategies";
import type { RunConfig, Engine } from "../types";
import StrategyHelp from "./StrategyHelp";

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
  engine: Engine;
  strategy: StrategyId;
}

interface Props {
  onRun: (cfg: RunConfig) => void;
  running: boolean;
}

// Left-hand configuration panel. On submit it maps the antd form values to the
// RunConfig shape backtest-service expects and calls onRun.
export default function ConfigForm({ onRun, running }: Props) {
  const [form] = Form.useForm<FormValues>();
  const strategy = Form.useWatch("strategy", form);

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
      engine: v.engine,
      strategy: v.strategy,
    });
  };

  return (
    <Card title="Configuration" size="small">
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={{
          symbol: "AAPL",
          range: [dayjs("2024-01-01"), dayjs("2024-12-31")],
          initial_cash: 10000,
          min_confidence: 0.6,
          rebalance_days: 5,
          position_pct: 1.0,
          stop_pct: 4, // same defaults as decision-service (STOP_PCT / TARGET_PCT)
          target_pct: 8,
          engine: "simple",
          strategy: DEFAULT_STRATEGY,
        }}
      >
        <Form.Item name="strategy" label="Strategy">
          <Select options={STRATEGY_OPTIONS} popupMatchSelectWidth={false} listHeight={420} />
        </Form.Item>
        <StrategyHelp
          id={strategy}
          onApply={(g) => form.setFieldsValue({ rebalance_days: g.rebalance_days, stop_pct: g.stop_pct * 100, target_pct: g.target_pct * 100 })}
        />
        <Form.Item name="symbol" label="Symbol" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Form.Item name="range" label="Date range" rules={[{ required: true }]}>
          <DatePicker.RangePicker style={{ width: "100%" }} />
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
        <Form.Item name="engine" label="Engine">
          <Select
            options={[
              { value: "simple", label: "simple" },
              { value: "nautilus", label: "nautilus (scaffold)" },
            ]}
          />
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={running}>
          Run backtest
        </Button>
      </Form>
    </Card>
  );
}
