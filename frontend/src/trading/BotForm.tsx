import { Alert, Checkbox, Col, Divider, Form, Input, InputNumber, Modal, Radio, Row, Select, Tooltip } from "antd";
import { useEffect, useRef, useState } from "react";
import StrategyHelp from "../components/StrategyHelp";
import { DEFAULT_STRATEGY, STRATEGY_OPTIONS, strategyInfo, type StrategyId, type StrategyInfo } from "../strategies";
import { slotTimes } from "./format";
import type { BotCreate, BotUpdate, BrokerInfo, DecideAt, StrategyParams } from "./types";

// The form works in human units (percent, dollars); the API wants fractions. These fields are
// percentages on screen and fractions on the wire.
const PCT_FIELDS = ["min_confidence", "position_pct", "stop_pct", "target_pct", "fee_pct", "slippage_pct", "max_drawdown_pct"] as const;

interface FormValues {
  symbol: string;
  name?: string;
  broker: string;
  allocated_cash: number;
  strategy: StrategyId;
  rebalance_days: number;
  decide_at: DecideAt;
  // all in percent here
  min_confidence: number;
  position_pct: number;
  stop_pct: number;
  target_pct: number;
  fee_pct: number;
  slippage_pct: number;
  max_drawdown_pct: number;
  confirm_live?: boolean;
}

/** Defaults match the backtest's defaults, so an untouched form = the untouched backtest. */
export const DEFAULT_PARAMS: StrategyParams = {
  strategy: DEFAULT_STRATEGY,
  min_confidence: 0.6,
  rebalance_days: 5,
  decide_at: "close", // the backtest's default too
  position_pct: 1.0,
  stop_pct: 0.04,
  target_pct: 0.08,
  fee_pct: 0,
  slippage_pct: 0.0005,
  max_drawdown_pct: 0.2,
};

export type BotFormInitial = Partial<BotCreate>;

interface Props {
  open: boolean;
  editing: boolean; // edit mode: symbol, broker and capital are fixed for a bot's life
  initial: BotFormInitial;
  brokers: BrokerInfo[];
  times?: { open: string; close: string }; // decision times in New York (from the service's /status)
  onCancel: () => void;
  onSubmit: (body: BotCreate | BotUpdate) => Promise<void>;
}

function toForm(v: BotFormInitial): Partial<FormValues> {
  const out: Record<string, unknown> = { broker: "paper", allocated_cash: 10_000, ...DEFAULT_PARAMS, ...v };
  for (const f of PCT_FIELDS) out[f] = +(((out[f] as number) ?? 0) * 100).toFixed(4);
  return out as Partial<FormValues>;
}

function fromForm(v: FormValues): BotCreate {
  const out: Record<string, unknown> = { ...v, symbol: v.symbol?.trim().toUpperCase(), name: v.name?.trim() || undefined };
  for (const f of PCT_FIELDS) out[f] = +((v[f] ?? 0) / 100).toFixed(6);
  return out as unknown as BotCreate;
}

export default function BotForm({ open, editing, initial, brokers, times = slotTimes(), onCancel, onSubmit }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const broker = Form.useWatch("broker", form);
  const strategy = Form.useWatch("strategy", form);
  const isLive = brokers.find((b) => b.name === broker)?.live ?? false;
  // A strategy's suggested rebalance, check time, stop and take-profit (percents on screen, fractions in the catalog).
  // Strategies without a suggested check time use the default, before the close.
  const applySuggested = (g: StrategyInfo["suggested"]) =>
    form.setFieldsValue({
      rebalance_days: g.rebalance_days,
      decide_at: g.decide_at ?? DEFAULT_PARAMS.decide_at,
      stop_pct: +(g.stop_pct * 100).toFixed(2),
      target_pct: +(g.target_pct * 100).toFixed(2),
    });

  // Load values only when the modal OPENS. `initial` is often the live bot object, which the page
  // re-polls every 15s; reacting to it would wipe whatever the user is in the middle of typing.
  const initialRef = useRef(initial);
  initialRef.current = initial;
  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue(toForm(initialRef.current));
      setError(null);
    }
  }, [open, form]);

  const submit = async () => {
    const values = await form.validateFields();
    setSaving(true);
    setError(null);
    try {
      const body = fromForm(values);
      if (editing) {
        // Only the tunable fields; the server ignores/forbids the rest anyway
        const { symbol: _s, broker: _b, allocated_cash: _c, confirm_live: _l, ...update } = body;
        await onSubmit(update);
      } else {
        await onSubmit(body);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const pctInput = (min: number, max: number, step: number) => (
    <InputNumber min={min} max={max} step={step} addonAfter="%" style={{ width: "100%" }} />
  );

  return (
    <Modal
      title={editing ? "Edit parameters" : "New trading bot"}
      open={open}
      onCancel={onCancel}
      onOk={submit}
      okText={editing ? "Save" : isLive ? "Create LIVE bot" : "Create bot"}
      okButtonProps={{ danger: isLive, loading: saving }}
      width={640}
      destroyOnClose
    >
      <Form<FormValues>
        form={form}
        layout="vertical"
        requiredMark={false}
        // Picking a strategy loads its suggested settings. Only a user's pick triggers this, not the values a
        // "Trade this strategy" prefill or an edited bot opens with, so those stay as they were.
        onValuesChange={(changed: Partial<FormValues>) => {
          const s = changed.strategy && strategyInfo(changed.strategy);
          if (s) applySuggested(s.suggested);
        }}
      >
        {!editing && (
          <>
            <Row gutter={12}>
              <Col span={8}>
                <Form.Item name="symbol" label="Symbol" rules={[{ required: true, pattern: /^[A-Za-z][A-Za-z.-]{0,9}$/, message: "e.g. AAPL" }]}>
                  <Input placeholder="AAPL" style={{ textTransform: "uppercase" }} />
                </Form.Item>
              </Col>
              <Col span={16}>
                <Form.Item name="name" label="Name (optional)" tooltip="To tell apart several bots on the same symbol">
                  <Input placeholder="e.g. AAPL careful" maxLength={80} />
                </Form.Item>
              </Col>
            </Row>
            <Row gutter={12}>
              <Col span={14}>
                <Form.Item name="broker" label="Broker">
                  <Select
                    options={brokers.map((b) => ({
                      value: b.name,
                      disabled: !b.available,
                      label: b.available ? b.label : <Tooltip title={b.reason}>{b.label} (not configured)</Tooltip>,
                    }))}
                  />
                </Form.Item>
              </Col>
              <Col span={10}>
                <Form.Item name="allocated_cash" label="Capital for this bot" tooltip="The bot never spends more than this; fixed for the bot's life">
                  <InputNumber min={100} step={1000} addonBefore="$" style={{ width: "100%" }} />
                </Form.Item>
              </Col>
            </Row>
          </>
        )}
        {editing && <Form.Item name="name" label="Name"><Input maxLength={80} /></Form.Item>}

        <Divider orientation="left" plain>Strategy</Divider>
        <Form.Item name="strategy" label="Strategy">
          <Select options={STRATEGY_OPTIONS} popupMatchSelectWidth={false} listHeight={420} />
        </Form.Item>
        <StrategyHelp id={strategy} onApply={applySuggested} />
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item name="min_confidence" label="Min confidence" tooltip="Signals weaker than this are ignored">
              {pctInput(0, 100, 5)}
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="rebalance_days" label="Every N trading days" tooltip="How often the bot decides: 1 = every trading day, 5 ≈ weekly. On those days it checks at the time(s) set in “Check at”.">
              <InputNumber min={1} max={60} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item
          name="decide_at"
          label="Check at (New York time)"
          tooltip={`When the bot asks for a signal on a decision day. Backtests can test all three (after the open: the last 60 days only). “After the open” skips the jumpy first 30 minutes and uses the same daily indicators with the ${times.open} price. “Both” checks twice: a morning BUY can be sold at ${times.close} (and the reverse).`}
        >
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            options={[
              { value: "close", label: `Before the close · ${times.close}` },
              { value: "open", label: `After the open · ${times.open}` },
              { value: "both", label: "Both" },
            ]}
          />
        </Form.Item>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="position_pct" label="Position size" tooltip="Share of the bot's cash spent on each BUY">
              {pctInput(1, 100, 10)}
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="stop_pct" label="Stop-loss" tooltip="Sell if the price falls this far below the entry fill">
              {pctInput(0.1, 50, 0.5)}
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="target_pct" label="Take-profit" tooltip="Sell if the price rises this far above the entry fill">
              {pctInput(0.1, 200, 0.5)}
            </Form.Item>
          </Col>
        </Row>

        <Divider orientation="left" plain>Safety &amp; costs</Divider>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="max_drawdown_pct" label="Drawdown breaker" tooltip="Auto-pause when equity falls this far below its peak (0 = off)">
              {pctInput(0, 100, 5)}
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="slippage_pct" label="Slippage buffer" tooltip="Paper: simulated slippage. Real brokers: cash kept aside when sizing so a slightly worse fill still fits">
              {pctInput(0, 5, 0.01)}
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="fee_pct" label="Fee (paper only)" tooltip="Simulated commission for the built-in paper broker">
              {pctInput(0, 5, 0.01)}
            </Form.Item>
          </Col>
        </Row>

        {editing && (
          <Alert type="info" showIcon message="Changes apply from the next decision. An open position keeps the stop-loss/target it was opened with." />
        )}
        {!editing && isLive && (
          <>
            <Alert
              type="error"
              showIcon
              message="This bot trades REAL MONEY"
              description="Run the same parameters on a paper broker for several weeks first and compare the results with your backtest."
              style={{ marginBottom: 12 }}
            />
            <Form.Item name="confirm_live" valuePropName="checked" rules={[{ validator: (_, v) => (v ? Promise.resolve() : Promise.reject(new Error("Required for a live bot"))) }]}>
              <Checkbox>I understand this bot places real orders with real money</Checkbox>
            </Form.Item>
          </>
        )}
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Form>
    </Modal>
  );
}
