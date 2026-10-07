import { Button, Card, Checkbox, DatePicker, Divider, Form, InputNumber, Select, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import UniverseField from "../rotation/UniverseField";
import PresetPicker from "./PresetPicker";
import RulesFields, { type RulesForm, fromRulesForm, toRulesForm } from "./RulesFields";
import { DEFAULT_RULES, type DipConfig, type DipPresetConfig, pickRules } from "./types";
import type { Period } from "./useDip";

const MUTED = "#8b98b5";

export const DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "LLY"];

interface FormValues extends RulesForm {
  symbols: string[];
  reenable_days: number;
  initial_cash: number;
  period_months: number; // 0 = the dates as picked
  practice: [Dayjs, Dayjs];
  exam_on: boolean;
  exam: [Dayjs, Dayjs];
}

const fmt = (d: Dayjs) => d.format("YYYY-MM-DD");

const MONTHS = [1, 2, 3, 4, 6, 9, 12, 18, 24, 36];
const monthsLabel = (n: number) => (n % 12 === 0 ? `${n / 12} year${n > 12 ? "s" : ""}` : `${n} month${n > 1 ? "s" : ""}`);
const spanWords = (n: number) => (n === 1 ? "month" : n === 12 ? "year" : monthsLabel(n)); // "the last month", "the last 3 months"

/** The periods for "the last n months": the exam is the last n months up to today and the practice the n months
 *  before it; without an exam, the practice is the last n months. */
function lastMonths(n: number, exam: boolean, today = dayjs()): Pick<FormValues, "practice"> & Partial<Pick<FormValues, "exam">> {
  const split = today.subtract(n, "month");
  return exam ? { practice: [today.subtract(2 * n, "month"), split.subtract(1, "day")], exam: [split, today] } : { practice: [split, today] };
}

function toConfigs(v: FormValues): Partial<Record<Period, DipConfig>> {
  const base = { ...fromRulesForm(v), symbols: v.symbols, reenable_days: v.reenable_days ?? 0, initial_cash: v.initial_cash };
  return {
    practice: { ...base, start: fmt(v.practice[0]), end: fmt(v.practice[1]) },
    ...(v.exam_on && { exam: { ...base, start: fmt(v.exam[0]), end: fmt(v.exam[1]) } }),
  };
}

/** The form as a setup to save. Also run on half-filled values (to compare with a loaded setup), so nothing is assumed. */
function toPreset(v: Partial<FormValues>): DipPresetConfig {
  const range = (r?: [Dayjs, Dayjs] | null) => (r?.[0] && r[1] ? ([fmt(r[0]), fmt(r[1])] as [string, string]) : null);
  return {
    ...fromRulesForm(v as RulesForm), symbols: v.symbols ?? [], reenable_days: v.reenable_days ?? 0, initial_cash: v.initial_cash ?? null,
    practice: range(v.practice), exam: v.exam_on === false ? null : range(v.exam), period_months: v.period_months || null,
  };
}

/** Left-hand panel of the Dip buyer page: the watchlist, the rules, and the practice and exam windows. */
export default function DipBacktestForm({ onRun, starting }: { onRun: (c: Partial<Record<Period, DipConfig>>) => void; starting: boolean }) {
  const [form] = Form.useForm<FormValues>();
  const v = Form.useWatch([], form) as Partial<FormValues> | undefined;
  const examOn = v?.exam_on !== false;
  const years = v?.practice ? ((examOn && v.exam ? v.exam[1] : v.practice[1]).diff(v.practice[0], "day")) / 365 : 2;
  const intraday = (v?.interval ?? "15m") !== "1d";
  const symbols = v?.symbols?.length ?? 0;

  const load = (c: DipPresetConfig) =>
    form.setFieldsValue({
      ...toRulesForm(pickRules(c)),
      symbols: c.symbols,
      reenable_days: c.reenable_days ?? 0,
      ...(c.initial_cash && { initial_cash: c.initial_cash }),
      // "The last n months" counts back from today, whenever it was saved; otherwise the dates as saved
      ...(c.period_months
        ? { period_months: c.period_months, exam_on: !!c.exam, ...lastMonths(c.period_months, !!c.exam) }
        : c.practice && {
            period_months: 0,
            practice: [dayjs(c.practice[0]), dayjs(c.practice[1])] as [Dayjs, Dayjs],
            exam_on: !!c.exam,
            ...(c.exam && { exam: [dayjs(c.exam[0]), dayjs(c.exam[1])] as [Dayjs, Dayjs] }),
          }),
    });

  /** Picking "the last n months" (or the exam on/off with it) sets the dates; changing a date by hand makes them custom. */
  const onValuesChange = (changed: Partial<FormValues>, all: FormValues) => {
    if ("period_months" in changed || "exam_on" in changed) {
      if (all.period_months) form.setFieldsValue(lastMonths(all.period_months, all.exam_on !== false));
    } else if ("practice" in changed || "exam" in changed) {
      form.setFieldsValue({ period_months: 0 });
    }
  };

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
        onValuesChange={onValuesChange}
        initialValues={{
          symbols: DEFAULT_SYMBOLS,
          ...toRulesForm(DEFAULT_RULES),
          reenable_days: 0,
          initial_cash: 10_000,
          period_months: 12,
          ...lastMonths(12, true),
          exam_on: true,
        }}
      >
        <Form.Item label="Saved setups" tooltip="Liked a result? Save these settings under a name, then pick the name here any time to fill them all back in. Also offered when you create or edit a dip bot.">
          <PresetPicker onLoad={load} values={v ? toPreset(v) : null} validate={async () => toPreset(await form.validateFields())} />
        </Form.Item>
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
        <Form.Item
          name="period_months"
          label="Test the last"
          tooltip="3 months: the exam is the last 3 months up to today, the practice the 3 months before it (6 to 3 months ago). Without an exam, the practice is the last 3 months. Change a date below to pick your own."
          extra={
            v?.period_months
              ? examOn
                ? `Exam: the last ${spanWords(v.period_months)}. Practice: the ${spanWords(v.period_months)} before.`
                : `Practice: the last ${spanWords(v.period_months)}.`
              : undefined
          }
        >
          <Select options={[...MONTHS.map((n) => ({ value: n, label: monthsLabel(n) })), { value: 0, label: "Custom dates" }]} />
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
