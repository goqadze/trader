import { Alert, Col, Divider, Form, Input, InputNumber, Modal, Row, Select, Tooltip, Typography } from "antd";
import { useEffect, useRef, useState } from "react";
import UniverseField from "../rotation/UniverseField";
import type { BrokerInfo } from "../trading/types";
import PresetPicker from "./PresetPicker";
import RulesFields, { type RulesForm, fromRulesForm, toRulesForm } from "./RulesFields";
import { DEFAULT_RULES, type DipBotCreate, type DipPresetConfig, pickRules } from "./types";

const MUTED = "#8b98b5";
const COSTS = ["fee_pct", "slippage_pct", "max_drawdown_pct"] as const;

interface FormValues extends RulesForm {
  name?: string;
  broker: string;
  allocated_cash: number;
  symbols: string[];
  fee_pct: number;
  slippage_pct: number;
  max_drawdown_pct: number;
}

export type DipBotInitial = Partial<DipBotCreate>;

const DEFAULTS: DipBotCreate = {
  ...DEFAULT_RULES,
  broker: "paper",
  allocated_cash: 10_000,
  symbols: [],
  fee_pct: 0,
  slippage_pct: 0.0005,
  max_drawdown_pct: 0.2,
};

function toForm(v: DipBotInitial): FormValues {
  const all = { ...DEFAULTS, ...v };
  const out = { ...all, ...toRulesForm(all) } as FormValues;
  for (const f of COSTS) out[f] = +(all[f] * 100).toFixed(4);
  return out;
}

function fromForm(v: FormValues): DipBotCreate {
  const out: DipBotCreate = {
    ...fromRulesForm(v), name: v.name?.trim() || undefined, broker: v.broker, allocated_cash: v.allocated_cash, symbols: v.symbols,
    fee_pct: 0, slippage_pct: 0, max_drawdown_pct: 0,
  };
  for (const f of COSTS) out[f] = +((v[f] ?? 0) / 100).toFixed(6);
  return out;
}

interface Props {
  open: boolean;
  initial: DipBotInitial;
  brokers: BrokerInfo[];
  onCancel: () => void;
  onSubmit: (body: DipBotCreate) => Promise<void>;
}

/** New dip buyer: the watchlist, the rules (prefilled from a backtest), a paper broker and the capital. */
export default function DipBotForm({ open, initial, brokers, onCancel, onSubmit }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load values only when the modal opens (see BotForm)
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
      await onSubmit(fromForm(values));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const pctInput = (min: number, max: number, step: number) => <InputNumber min={min} max={max} step={step} addonAfter="%" style={{ width: "100%" }} />;

  const load = (c: DipPresetConfig) => {
    form.setFieldsValue({ ...toRulesForm(pickRules(c)), symbols: c.symbols, ...(c.initial_cash && { allocated_cash: c.initial_cash }) });
    void form.validateFields(["lookback"]).catch(() => undefined); // a backtest's window may be too long for a bot: say so now
  };

  return (
    <Modal title="New dip buyer" open={open} onCancel={onCancel} onOk={submit} okText="Create bot" okButtonProps={{ loading: saving }} width={680} destroyOnClose>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        It checks every symbol on its watchlist at each interval: buys the ones that just fell, sells them once they're back (or at
        the stop-loss, which also blacklists the symbol), and posts every signal to the bell at the top. You can add and remove symbols,
        re-enable blacklisted ones and switch the news on or off while it runs. Paper accounts only for now.
      </Typography.Paragraph>
      <Form<FormValues> form={form} layout="vertical" requiredMark={false}>
        <Form.Item label="Start from a saved setup" tooltip="Fills in its rules, watchlist and capital (save setups on the Backtest tab)">
          <PresetPicker onLoad={load} />
        </Form.Item>
        <Row gutter={12}>
          <Col span={14}>
            <Form.Item name="broker" label="Broker">
              <Select
                options={brokers.filter((b) => !b.live).map((b) => ({
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
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="Only want the recommendations?"
          description="Pick Paper (built-in simulator): nothing real is bought, and you still get every down / up / stop signal, with a sound or read aloud if you like (the bell at the top)."
        />
        <Form.Item name="name" label="Name (optional)">
          <Input placeholder="e.g. Big tech dips" maxLength={80} />
        </Form.Item>
        <UniverseField name="symbols" label="Watchlist" noCrypto min={1} />

        <Divider orientation="left" plain>Rules</Divider>
        <RulesFields forBot />

        <Divider orientation="left" plain>Safety &amp; costs</Divider>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="max_drawdown_pct" label="Drawdown breaker" tooltip="Pause (no more buys) when equity falls this far below its peak; holdings still exit. 0 = off">
              {pctInput(0, 100, 5)}
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="slippage_pct" label="Slippage buffer" tooltip="Paper: simulated slippage. Alpaca: cash kept aside when sizing so a slightly worse fill still fits">
              {pctInput(0, 5, 0.01)}
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="fee_pct" label="Fee (paper only)" tooltip="Simulated commission for the built-in paper broker">
              {pctInput(0, 5, 0.01)}
            </Form.Item>
          </Col>
        </Row>
        <Alert
          type="warning"
          showIcon
          message="The stop-loss is checked at each check, not held at the broker"
          description="A fast fall between two checks (or overnight) sells lower than the stop. Shorter intervals react faster."
        />
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Form>
    </Modal>
  );
}
