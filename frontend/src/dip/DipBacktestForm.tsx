import { Button, Card, Checkbox, DatePicker, Divider, Form, InputNumber, Select, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useEffect } from "react";
import { lastMonths, periodExtra, periodOptions } from "../periods";
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
interface Props {
  onRun: (c: Partial<Record<Period, DipConfig>>) => void;
  starting: boolean;
  /** A setup to fill the form with (the Test results tab's "Load into the backtest form"); a new seq loads it again. */
  loadRequest?: { config: DipPresetConfig; seq: number } | null;
}

export default function DipBacktestForm({ onRun, starting, loadRequest }: Props) {
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

  useEffect(() => {
    if (loadRequest) load(loadRequest.config);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new request loads
  }, [loadRequest?.seq]);

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
        <Form.Item name="initial_cash" label="Capital">
          <InputNumber min={100} step={100} addonBefore="$" style={{ width: "100%" }} />
        </Form.Item>
        <Divider style={{ margin: "8px 0 12px" }} />
        <Form.Item
          name="period_months"
          label="Test the last"
          tooltip="3 months: the exam is the last 3 months up to today, the practice the 3 months before it (6 to 3 months ago). Without an exam, the practice is the last 3 months. Change a date below to pick your own."
          extra={periodExtra(v?.period_months, examOn)}
        >
          <Select options={periodOptions()} />
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
