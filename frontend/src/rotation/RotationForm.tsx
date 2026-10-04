import { Button, Card, Checkbox, DatePicker, Form, InputNumber, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import type { Period } from "./useRotation";
import type { RotationConfig } from "./types";
import UniverseField, { SECTORS } from "./UniverseField";

const MUTED = "#8b98b5";

interface FormValues {
  symbols: string[];
  top_n: number;
  lookback_months: number;
  skip_month: boolean;
  abs_filter: boolean;
  practice: [Dayjs, Dayjs];
  exam_on: boolean;
  exam: [Dayjs, Dayjs];
}

const fmt = (d: Dayjs) => d.format("YYYY-MM-DD");

function toConfigs(v: FormValues): Partial<Record<Period, RotationConfig>> {
  const base = { symbols: v.symbols, top_n: v.top_n, lookback_months: v.lookback_months, skip_months: v.skip_month ? 1 : 0, abs_filter: v.abs_filter };
  return {
    practice: { ...base, start: fmt(v.practice[0]), end: fmt(v.practice[1]) },
    ...(v.exam_on && { exam: { ...base, start: fmt(v.exam[0]), end: fmt(v.exam[1]) } }),
  };
}

/** Left-hand panel of the rotation: the universe, how many to hold, the momentum, and the two windows. */
export default function RotationForm({ onRun, starting }: { onRun: (c: Partial<Record<Period, RotationConfig>>) => void; starting: boolean }) {
  const [form] = Form.useForm<FormValues>();
  const v = Form.useWatch([], form) as Partial<FormValues> | undefined;
  const today = dayjs();
  return (
    <Card title="Momentum rotation" size="small">
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        Each month, hold the strongest few of a group of symbols (by their return over the past year, skipping the latest
        month) in equal parts, and sell the rest. Judged against holding the whole group in equal parts.
      </Typography.Paragraph>
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={(values) => onRun(toConfigs(values))}
        initialValues={{
          symbols: SECTORS,
          top_n: 3,
          lookback_months: 12,
          skip_month: true,
          abs_filter: true,
          practice: [today.subtract(6, "year"), today.subtract(3, "year").subtract(1, "day")],
          exam_on: true,
          exam: [today.subtract(3, "year"), today],
        }}
      >
        <UniverseField name="symbols" />
        <Form.Item
          name="top_n"
          label="Hold the strongest"
          dependencies={["symbols"]}
          rules={[({ getFieldValue }) => ({
            validator: (_, n: number) => (n <= (getFieldValue("symbols")?.length ?? 0) ? Promise.resolve() : Promise.reject(new Error("More than the universe"))),
          })]}
        >
          <InputNumber min={1} max={20} addonAfter="symbols" style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="lookback_months" label="Momentum over the past" tooltip="Ranked by the return over this many months. 12 is the classic; 3 or 6 react faster.">
          <InputNumber min={1} max={24} addonAfter="months" style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="skip_month" valuePropName="checked" style={{ marginBottom: 8 }}>
          <Checkbox>
            Skip the latest month <span style={{ color: MUTED, fontSize: 12 }}>(the classic 12-1: last month's winners tend to slip back)</span>
          </Checkbox>
        </Form.Item>
        <Form.Item name="abs_filter" valuePropName="checked">
          <Checkbox>
            Only hold what rose <span style={{ color: MUTED, fontSize: 12 }}>(otherwise that slot waits in cash: some protection in a crash)</span>
          </Checkbox>
        </Form.Item>
        <Form.Item name="practice" label="Practice period" rules={[{ required: true }]}>
          <DatePicker.RangePicker style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="exam_on" valuePropName="checked" style={{ marginBottom: 8 }}>
          <Checkbox>Exam on unseen data after it</Checkbox>
        </Form.Item>
        {v?.exam_on !== false && (
          <Form.Item
            name="exam"
            label="Exam period"
            dependencies={["practice"]}
            rules={[
              { required: true },
              ({ getFieldValue }) => ({
                validator: (_, exam?: [Dayjs, Dayjs]) =>
                  !exam || !getFieldValue("practice") || exam[0].isAfter(getFieldValue("practice")[1], "day")
                    ? Promise.resolve()
                    : Promise.reject(new Error("The exam must start after the practice period ends")),
              }),
            ]}
          >
            <DatePicker.RangePicker style={{ width: "100%" }} />
          </Form.Item>
        )}
        <Button type="primary" htmlType="submit" block loading={starting}>
          Run rotation
        </Button>
        <Typography.Text style={{ display: "block", marginTop: 8, fontSize: 12, color: MUTED }}>
          Trades at each month's last close, with 0.05% slippage. Takes a few seconds.
        </Typography.Text>
      </Form>
    </Card>
  );
}
