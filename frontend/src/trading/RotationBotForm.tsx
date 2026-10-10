import { Alert, Checkbox, Col, Divider, Form, Input, InputNumber, Modal, Row, Select, Switch, Tooltip, Typography } from "antd";
import { useEffect, useRef, useState } from "react";
import UniverseField, { SECTORS } from "../rotation/UniverseField";
import type { BrokerInfo, RotationBotCreate } from "./types";

const MUTED = "#8b98b5";

// Percent on screen, fractions on the wire (like BotForm)
const PCT_FIELDS = ["fee_pct", "slippage_pct", "max_drawdown_pct"] as const;

interface FormValues {
  name?: string;
  broker: string;
  allocated_cash: number;
  universe: string[];
  top_n: number;
  lookback_months: number;
  skip_month: boolean;
  abs_filter: boolean;
  fractional: boolean;
  fee_pct: number;
  slippage_pct: number;
  max_drawdown_pct: number;
}

export type RotationBotInitial = Partial<RotationBotCreate>;

/** The backtest's defaults, so an untouched form = the untouched rotation backtest. */
const DEFAULTS: RotationBotCreate = {
  broker: "paper",
  allocated_cash: 10_000,
  universe: SECTORS,
  top_n: 3,
  lookback_months: 12,
  skip_months: 1,
  abs_filter: true,
  fractional: true,
  fee_pct: 0,
  slippage_pct: 0.0005,
  max_drawdown_pct: 0.2,
};

function toForm(v: RotationBotInitial): FormValues {
  const all = { ...DEFAULTS, ...v };
  return {
    ...all,
    skip_month: all.skip_months > 0,
    fee_pct: +(all.fee_pct * 100).toFixed(4),
    slippage_pct: +(all.slippage_pct * 100).toFixed(4),
    max_drawdown_pct: +(all.max_drawdown_pct * 100).toFixed(4),
  };
}

function fromForm(v: FormValues): RotationBotCreate {
  const { skip_month, ...rest } = v;
  const out = { ...rest, name: v.name?.trim() || undefined, skip_months: skip_month ? 1 : 0 };
  for (const f of PCT_FIELDS) out[f] = +((v[f] ?? 0) / 100).toFixed(6);
  return out;
}

interface Props {
  open: boolean;
  initial: RotationBotInitial;
  brokers: BrokerInfo[];
  onCancel: () => void;
  onSubmit: (body: RotationBotCreate) => Promise<void>;
}

/** New momentum rotation bot: the rotation's rules (prefilled from a rotation backtest), a paper broker and the capital. */
export default function RotationBotForm({ open, initial, brokers, onCancel, onSubmit }: Props) {
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

  return (
    <Modal title="New rotation bot" open={open} onCancel={onCancel} onOk={submit} okText="Create bot" okButtonProps={{ loading: saving }} width={640} destroyOnClose>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        On each month's last trading day, 30 minutes before the close, it ranks the universe by momentum and holds the strongest
        few in equal parts, selling what dropped out: the same rules as the rotation backtest. It starts with a rebalance at the
        next decision time. Paper accounts only for now.
      </Typography.Paragraph>
      <Form<FormValues> form={form} layout="vertical" requiredMark={false}>
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
            <Form.Item name="allocated_cash" label="Capital for this bot" tooltip="The bot never spends more than this. You can add more later (Add money on its page)">
              <InputNumber min={100} step={1000} addonBefore="$" style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="name" label="Name (optional)">
          <Input placeholder="e.g. Sector rotation" maxLength={80} />
        </Form.Item>

        <Divider orientation="left" plain>Rotation</Divider>
        <UniverseField name="universe" noCrypto />
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item
              name="top_n"
              label="Hold the strongest"
              dependencies={["universe"]}
              rules={[({ getFieldValue }) => ({
                validator: (_, n: number) => (n <= (getFieldValue("universe")?.length ?? 0) ? Promise.resolve() : Promise.reject(new Error("More than the universe"))),
              })]}
            >
              <InputNumber min={1} max={20} addonAfter="symbols" style={{ width: "100%" }} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="lookback_months" label="Momentum over the past">
              <InputNumber min={1} max={24} addonAfter="months" style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="skip_month" valuePropName="checked" style={{ marginBottom: 8 }}>
          <Checkbox>Skip the latest month <span style={{ color: MUTED, fontSize: 12 }}>(the classic 12-1)</span></Checkbox>
        </Form.Item>
        <Form.Item name="abs_filter" valuePropName="checked">
          <Checkbox>Only hold what rose <span style={{ color: MUTED, fontSize: 12 }}>(otherwise that slot waits in cash)</span></Checkbox>
        </Form.Item>
        <Form.Item name="fractional" valuePropName="checked" style={{ marginBottom: 4 }}>
          <Switch checkedChildren="Fractional shares" unCheckedChildren="Whole shares" />
        </Form.Item>
        <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
          Fractional: buys part of a share when a slot is smaller than one share's price, so a small account still holds equal
          parts like the backtest. Alpaca allows it for most US stocks and ETFs, from $1; a symbol it can't split is bought in
          whole shares.
        </Typography.Paragraph>

        <Divider orientation="left" plain>Safety &amp; costs</Divider>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="max_drawdown_pct" label="Drawdown breaker" tooltip="Pause (no more rebalances) when equity falls this far below its peak; 0 = off">
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
          type="info"
          showIcon
          message="No stop-loss on a rotation"
          description="Like the backtest, it only acts at month ends. Between them it holds through drops; the breaker pauses it if the loss gets too big."
        />
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Form>
    </Modal>
  );
}
