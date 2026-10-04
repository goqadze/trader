import { Alert, Button, Card, Checkbox, DatePicker, Form, Select, Space, Switch, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { STRATEGIES, strategyInfo, type StrategyId } from "../strategies";
import { SYMBOL_GROUPS, TWINS } from "../symbols";
import type { RunSettings, ScanConfig } from "./types";

const MUTED = "#8b98b5";
const STYLES = ["trend", "momentum", "breakout", "mean reversion", "event"] as const;
const NEEDS_NEWS: StrategyId = "news_catalyst"; // trades on news alone: nothing to do in a technical-only scan

interface FormValues {
  symbols: string[];
  strategies: StrategyId[];
  news: boolean;
  practice: [Dayjs, Dayjs];
  exam_on: boolean;
  exam: [Dayjs, Dayjs];
  exam_all: boolean;
}

const SYMBOL_OPTIONS = SYMBOL_GROUPS.map((g) => ({
  label: g.label,
  options: g.symbols.map((s) => ({ value: s.symbol, label: `${s.symbol} · ${s.name}` })),
}));

const fmt = (d: Dayjs) => d.format("YYYY-MM-DD");

const MAX_SYMBOLS = 60; // backtest-service's ScanConfig limit
// Measured: technical only, 3 runs at a time, a backtest-year takes about this long (data downloads included).
// With news each decision waits for the news step, many times slower.
const SECONDS_PER_RUN_YEAR = 3.5;
const years = (r?: [Dayjs, Dayjs]) => (r?.[0] && r?.[1] ? r[1].diff(r[0], "day") / 365.25 : 0);

/** "about 25 minutes" for the practice runs (plus every exam run when they all sit it), technical only. */
function estimate(v: Partial<FormValues>, combos: number): string | null {
  if (!combos || v.news) return null;
  const runYears = combos * (years(v.practice) + (v.exam_on !== false && v.exam_all ? years(v.exam) : 0));
  const min = Math.max(1, Math.round((runYears * SECONDS_PER_RUN_YEAR) / 60));
  return min < 90 ? `about ${min} minute${min === 1 ? "" : "s"}` : `about ${(min / 60).toFixed(1)} hours`;
}

/** Each strategy on its own suggested settings (like Compare's default), with the same cash, sizing and breaker. */
function settingsFor(id: StrategyId, news: boolean): RunSettings {
  const g = strategyInfo(id)!.suggested;
  return {
    strategy: id,
    initial_cash: 10000,
    min_confidence: 0.6,
    position_pct: 1.0,
    rebalance_days: g.rebalance_days,
    stop_pct: g.stop_pct,
    target_pct: g.target_pct,
    decide_at: g.decide_at ?? "close",
    max_drawdown_pct: 0.2,
    news,
  };
}

function toConfig(v: FormValues): ScanConfig {
  const ids = v.strategies.filter((id) => v.news || id !== NEEDS_NEWS);
  return {
    symbols: v.symbols.map((s) => s.trim().toUpperCase()),
    runs: ids.map((id) => settingsFor(id, v.news)),
    practice: { start: fmt(v.practice[0]), end: fmt(v.practice[1]) },
    exam: v.exam_on ? { start: fmt(v.exam[0]), end: fmt(v.exam[1]) } : null,
    exam_all: v.exam_all,
  };
}

/** The twin families with more than one member picked, e.g. [{label: "the S&P 500", picked: ["SPY", "VOO"]}]. */
function pickedTwins(symbols: string[]) {
  return TWINS.map((t) => ({ label: t.label, picked: t.symbols.filter((s) => symbols.includes(s)) })).filter((t) => t.picked.length > 1);
}

const andList = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

interface Props {
  onRun: (cfg: ScanConfig) => void;
  starting: boolean;
}

/** Left-hand panel of a scan: which symbols and strategies, technical only or with news, and the two windows. */
export default function ScanForm({ onRun, starting }: Props) {
  const [form] = Form.useForm<FormValues>();
  const v = Form.useWatch([], form) as Partial<FormValues> | undefined;
  const news = v?.news ?? false;
  const nStrategies = (v?.strategies ?? []).filter((id) => news || id !== NEEDS_NEWS).length;
  const combos = (v?.symbols?.length ?? 0) * nStrategies;
  const est = estimate(v ?? {}, combos);
  const twins = pickedTwins(v?.symbols ?? []);
  // Keep the first picked member of each family (the order in TWINS: the most traded first)
  const keepOneOfEach = () => {
    const extra = new Set(twins.flatMap((t) => t.picked.slice(1)));
    form.setFieldValue("symbols", (form.getFieldValue("symbols") as string[]).filter((s) => !extra.has(s)));
  };
  const addSymbols = (more: string[]) => form.setFieldValue("symbols", [...new Set([...(form.getFieldValue("symbols") ?? []), ...more])]);
  const today = dayjs();

  return (
    <Card title="Scan" size="small">
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={(values) => onRun(toConfig(values))}
        initialValues={{
          symbols: ["QQQ", "SPY", "DIA", "IWM"],
          strategies: STRATEGIES.map((s) => s.id).filter((id) => id !== NEEDS_NEWS),
          news: false,
          // Practice on three older years (2022's bear market included), sit the exam on the last three
          practice: [today.subtract(6, "year"), today.subtract(3, "year").subtract(1, "day")],
          exam_on: true,
          exam: [today.subtract(3, "year"), today],
          exam_all: false,
        }}
      >
        <Form.Item
          label="Symbols"
          extra={
            <Space size={0} wrap>
              {SYMBOL_GROUPS.map((g) => (
                <Button key={g.label} type="link" size="small" style={{ paddingInline: 4 }} onClick={() => addSymbols(g.symbols.map((s) => s.symbol))}>
                  + {g.short}
                </Button>
              ))}
            </Space>
          }
        >
          <Form.Item
            name="symbols"
            noStyle
            rules={[
              { required: true, type: "array", min: 1, message: "Pick at least one symbol" },
              { type: "array", max: MAX_SYMBOLS, message: `At most ${MAX_SYMBOLS} symbols in one scan` },
            ]}
            normalize={(vals: string[]) => [...new Set(vals.map((s) => s.trim().toUpperCase()).filter(Boolean))]}
          >
            <Select mode="tags" options={SYMBOL_OPTIONS} optionLabelProp="value" tokenSeparators={[",", " "]} placeholder="Pick or type tickers" />
          </Form.Item>
        </Form.Item>

        {twins.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: -12, marginBottom: 16, fontSize: 12 }}
            message="Twin ETFs"
            description={
              <>
                {twins.map((t) => (
                  <div key={t.label}>
                    {andList(t.picked)} {t.picked.length === 2 ? "both" : "all"} track {t.label} and move almost the same.
                  </div>
                ))}
                Scanning twins repeats one result and makes one idea look like it worked on several symbols.{" "}
                <Button type="link" size="small" style={{ padding: 0, fontSize: 12 }} onClick={keepOneOfEach}>
                  Keep one of each
                </Button>
              </>
            }
          />
        )}

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
          <Form.Item name="strategies" noStyle rules={[{ required: true, type: "array", min: 1, message: "Pick at least one strategy" }]}>
            <Checkbox.Group style={{ display: "block" }}>
              {STYLES.map((style) => (
                <div key={style} style={{ marginBottom: 6 }}>
                  <div style={{ fontSize: 11, color: MUTED, textTransform: "uppercase", letterSpacing: 0.5 }}>{style}</div>
                  {STRATEGIES.filter((s) => s.style === style).map((s) => (
                    <div key={s.id}>
                      <Checkbox value={s.id} title={s.summary} disabled={!news && s.id === NEEDS_NEWS}>
                        {s.name}
                        {!news && s.id === NEEDS_NEWS && <span style={{ color: MUTED, fontSize: 12 }}> (needs news)</span>}
                      </Checkbox>
                    </div>
                  ))}
                </div>
              ))}
            </Checkbox.Group>
          </Form.Item>
        </Form.Item>

        <Form.Item
          name="news"
          label="News"
          valuePropName="checked"
          extra={
            news
              ? "Each decision reads the news like a live bot: slow (hours for a big scan) and uses OpenAI tokens."
              : "Technical only: decided on the price alone. Fast and free; re-check the finalists with news afterwards."
          }
        >
          <Switch checkedChildren="with news" unCheckedChildren="technical only" />
        </Form.Item>

        <Form.Item name="practice" label="Practice period" rules={[{ required: true }]} tooltip="Where the strategies are tried. Include a bad year (2022) so you see how each handles a falling market.">
          <DatePicker.RangePicker style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="exam_on" valuePropName="checked" style={{ marginBottom: 8 }}>
          <Checkbox>Exam on unseen data after it</Checkbox>
        </Form.Item>
        {v?.exam_on !== false && (
          <>
            <Form.Item
              name="exam"
              label="Exam period"
              tooltip="A later window the practice never saw. Only what passes here too is a real candidate."
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
            <Form.Item name="exam_all" valuePropName="checked">
              <Checkbox>
                Examine every combination <span style={{ color: MUTED, fontSize: 12 }}>(default: only those that passed practice)</span>
              </Checkbox>
            </Form.Item>
          </>
        )}

        <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
          Each strategy runs on its own suggested settings, with $10,000, full position size and the 20% breaker.
        </Typography.Paragraph>
        <Button type="primary" htmlType="submit" block loading={starting} disabled={!combos}>
          Scan {combos} combination{combos === 1 ? "" : "s"}
        </Button>
        <Typography.Text style={{ display: "block", marginTop: 8, fontSize: 12, color: MUTED }}>
          {combos} practice backtest{combos === 1 ? "" : "s"}
          {v?.exam_on !== false && (v?.exam_all ? `, then ${combos} exam backtests` : ", then an exam backtest for each that passes")}.
          {est && ` Technical only: ${est}${v?.exam_on !== false && !v?.exam_all ? " plus the exams" : ""}.`}{" "}
          A few run at a time, on the server: you can leave this page.
        </Typography.Text>
      </Form>
    </Card>
  );
}
