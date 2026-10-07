import { Button, Card, Checkbox, DatePicker, Divider, Form, InputNumber, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import UniverseField from "../rotation/UniverseField";
import RulesFields, { type RulesForm, fromRulesForm, toRulesForm } from "./RulesFields";
import { DEFAULT_RULES, type DipConfig } from "./types";
import type { Period } from "./useDip";

const MUTED = "#8b98b5";

export const DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "LLY"];

interface FormValues extends RulesForm {
  symbols: string[];
  reenable_days: number;
  initial_cash: number;
  practice: [Dayjs, Dayjs];
  exam_on: boolean;
  exam: [Dayjs, Dayjs];
}

const fmt = (d: Dayjs) => d.format("YYYY-MM-DD");

function toConfigs(v: FormValues): Partial<Record<Period, DipConfig>> {
  const base = { ...fromRulesForm(v), symbols: v.symbols, reenable_days: v.reenable_days ?? 0, initial_cash: v.initial_cash };
  return {
    practice: { ...base, start: fmt(v.practice[0]), end: fmt(v.practice[1]) },
    ...(v.exam_on && { exam: { ...base, start: fmt(v.exam[0]), end: fmt(v.exam[1]) } }),
  };
}

/** Left-hand panel of the Dip buyer page: the watchlist, the rules, and the practice and exam windows. */
export default function DipBacktestForm({ onRun, starting }: { onRun: (c: Partial<Record<Period, DipConfig>>) => void; starting: boolean }) {
  const [form] = Form.useForm<FormValues>();
  const v = Form.useWatch([], form) as Partial<FormValues> | undefined;
  const today = dayjs();
  const years = v?.practice && v?.exam ? (v.exam_on ? v.exam[1].diff(v.practice[0], "day") : v.practice[1].diff(v.practice[0], "day")) / 365 : 2;
  const intraday = (v?.interval ?? "15m") !== "1d";
  const symbols = v?.symbols?.length ?? 0;
  return (
    <Card title="Dip buyer" size="small">
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        Watch a list of symbols; when one falls x% during the last y days, buy it and sell when it is back where the fall started.
        A stop-loss z% under the buy sells it and blacklists the symbol. Judged against holding all of them in equal parts.
      </Typography.Paragraph>
      <Form<FormValues>
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={(values) => onRun(toConfigs(values))}
        initialValues={{
          symbols: DEFAULT_SYMBOLS,
          ...toRulesForm(DEFAULT_RULES),
          reenable_days: 0,
          initial_cash: 10_000,
          practice: [today.subtract(2, "year"), today.subtract(1, "year").subtract(1, "day")],
          exam_on: true,
          exam: [today.subtract(1, "year"), today],
        }}
      >
        <UniverseField name="symbols" label="Watchlist" noCrypto min={1} />
        <RulesFields compact />
        <Form.Item
          name="reenable_days"
          label="Blacklist lasts"
          tooltip="A live bot keeps a symbol blacklisted until you re-enable it. In a backtest nobody does, unless you set this: it re-enables a blacklisted symbol after this many trading days, as if you did."
        >
          <InputNumber min={0} max={500} addonAfter="trading days (0 = for good)" style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="initial_cash" label="Capital">
          <InputNumber min={1000} step={1000} addonBefore="$" style={{ width: "100%" }} />
        </Form.Item>
        <Divider style={{ margin: "8px 0 12px" }} />
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
          Run backtest
        </Button>
        <Typography.Text style={{ display: "block", marginTop: 8, fontSize: 12, color: MUTED }}>
          {intraday
            ? `Replays every check on intraday bars (0.05% slippage per fill). The first run downloads them: roughly ${Math.max(1, Math.round(symbols * years * 0.12))} min for ${symbols} symbols over ${years.toFixed(1)} years, then seconds.`
            : "Replays one check a day on the daily closes (0.05% slippage per fill). Takes seconds, even over 10+ years."}
        </Typography.Text>
      </Form>
    </Card>
  );
}
