import { Form, Input, InputNumber, DatePicker, Select, Button, Card } from "antd";
import dayjs, { Dayjs } from "dayjs";
import type { RunConfig, Engine, Mode } from "../types";

// antd form values (dates are dayjs objects; we format them on submit).
interface FormValues {
  symbol: string;
  range: [Dayjs, Dayjs];
  initial_cash: number;
  min_confidence: number;
  rebalance_days: number;
  position_pct: number;
  engine: Engine;
  mode: Mode;
}

interface Props {
  onRun: (cfg: RunConfig) => void;
  running: boolean;
}

// Left-hand configuration panel. On submit it maps the antd form values to the
// RunConfig shape backtest-service expects and calls onRun.
export default function ConfigForm({ onRun, running }: Props) {
  const [form] = Form.useForm<FormValues>();

  const submit = (v: FormValues) => {
    onRun({
      symbol: v.symbol.trim().toUpperCase(),
      start: v.range[0].format("YYYY-MM-DD"),
      end: v.range[1].format("YYYY-MM-DD"),
      initial_cash: v.initial_cash,
      min_confidence: v.min_confidence,
      rebalance_days: v.rebalance_days,
      position_pct: v.position_pct,
      engine: v.engine,
      mode: v.mode,
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
          engine: "simple",
          mode: "rules",
        }}
      >
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
        <Form.Item name="engine" label="Engine">
          <Select
            options={[
              { value: "simple", label: "simple" },
              { value: "nautilus", label: "nautilus (scaffold)" },
            ]}
          />
        </Form.Item>
        <Form.Item name="mode" label="Decision mode">
          <Select
            options={[
              { value: "rules", label: "rules (SMA/RSI)" },
              { value: "llm", label: "llm (model decides)" },
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
