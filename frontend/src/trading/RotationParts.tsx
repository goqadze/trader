// The parts of a bot's page that differ for a momentum rotation bot: what it holds, its rules, and what can be edited.

import { Alert, Button, Card, Col, Descriptions, Form, Input, InputNumber, Modal, Row, Space, Switch, Table, Tag, Tooltip } from "antd";
import { useEffect, useRef, useState } from "react";
import { frac, nyTime, pct, pnlColor, usd } from "./format";
import type { Bot, Holding, RotationBotUpdate } from "./types";

const MUTED = "#8b98b5";

/** The holdings, valued at their last prices, plus the cash. */
export function HoldingsCard({ bot }: { bot: Bot }) {
  const cashPct = bot.equity ? (bot.cash / bot.equity) * 100 : 0;
  return (
    <Card
      title="Holdings"
      size="small"
      style={{ height: "100%" }}
      extra={bot.rebalancing && <Tooltip title="Sells first, then the buys once they've filled"><Tag color="processing">rebalancing</Tag></Tooltip>}
    >
      {bot.holdings.length === 0 ? (
        <Space direction="vertical" size={2}>
          <span>Holding cash only.</span>
          <span style={{ color: MUTED }}>
            {bot.next_decision_at
              ? `The next rebalance (${nyTime(bot.next_decision_at)}) buys the strongest ${bot.top_n} that rose.`
              : "Paused: no rebalances until it's resumed."}
          </span>
        </Space>
      ) : (
        <Table<Holding>
          rowKey="symbol"
          size="small"
          pagination={false}
          scroll={{ x: 560 }}
          dataSource={bot.holdings}
          columns={[
            { title: "Symbol", dataIndex: "symbol", render: (s: string) => <b>{s}</b> },
            { title: "Shares", dataIndex: "shares", align: "right" },
            { title: "Avg cost", key: "avg", align: "right", render: (_, h) => usd(h.cost_basis / h.shares) },
            { title: "Last", dataIndex: "last_price", align: "right", render: (v: number | null) => usd(v) },
            { title: "Value", dataIndex: "value", align: "right", render: (v: number) => usd(v, 0) },
            { title: "Weight", dataIndex: "weight_pct", align: "right", render: (v: number) => `${v.toFixed(1)}%` },
            {
              title: "P&L", dataIndex: "unrealized_pnl", align: "right",
              render: (v: number, h) => <span style={{ color: pnlColor(v) }}>{usd(v)} <span style={{ fontSize: 12 }}>({pct((v / h.cost_basis) * 100, 1)})</span></span>,
            },
          ]}
          summary={() => (
            <Table.Summary.Row>
              <Table.Summary.Cell index={0} colSpan={4}><span style={{ color: MUTED }}>Cash</span></Table.Summary.Cell>
              <Table.Summary.Cell index={4} align="right">{usd(bot.cash, 0)}</Table.Summary.Cell>
              <Table.Summary.Cell index={5} align="right">{cashPct.toFixed(1)}%</Table.Summary.Cell>
              <Table.Summary.Cell index={6} />
            </Table.Summary.Row>
          )}
        />
      )}
    </Card>
  );
}

/** The rotation's rules (fixed for the bot's life) and the settings that can change. */
export function RotationParamsCard({ bot, closeTime, onEdit }: { bot: Bot; closeTime: string; onEdit: () => void }) {
  return (
    <Card title="Parameters" size="small" style={{ height: "100%" }} extra={<Button size="small" type="link" onClick={onEdit}>Edit</Button>}>
      <Descriptions column={2} size="small">
        <Descriptions.Item label="Strategy" span={2}>Momentum rotation</Descriptions.Item>
        <Descriptions.Item label="Universe" span={2}>
          <Space size={[0, 4]} wrap>{bot.universe?.map((s) => <Tag key={s}>{s}</Tag>)}</Space>
        </Descriptions.Item>
        <Descriptions.Item label="Holds">the strongest {bot.top_n}, in equal parts</Descriptions.Item>
        <Descriptions.Item label="Momentum">
          {bot.lookback_months} months{bot.skip_months ? `, skipping the latest ${bot.skip_months === 1 ? "month" : `${bot.skip_months} months`}` : ""}
        </Descriptions.Item>
        <Descriptions.Item label="Only what rose">{bot.abs_filter ? "yes (else cash)" : "no"}</Descriptions.Item>
        <Descriptions.Item label="Shares">{bot.fractional ? "fractions where the broker allows" : "whole shares only"}</Descriptions.Item>
        <Descriptions.Item label="Rebalances">each month's last trading day, {closeTime} ET</Descriptions.Item>
        <Descriptions.Item label="Breaker">{bot.max_drawdown_pct ? `${frac(bot.max_drawdown_pct)} drawdown` : "off"}</Descriptions.Item>
        <Descriptions.Item label="Slippage · fee">{frac(bot.slippage_pct)} · {frac(bot.fee_pct)}</Descriptions.Item>
      </Descriptions>
    </Card>
  );
}

interface EditValues {
  name: string;
  slippage_pct: number;
  fee_pct: number;
  max_drawdown_pct: number;
  fractional: boolean;
}

/** Edit what a rotation bot can change: its name, costs and breaker. */
export function RotationEditForm({ open, bot, onCancel, onSubmit }: {
  open: boolean;
  bot: Bot;
  onCancel: () => void;
  onSubmit: (body: RotationBotUpdate) => Promise<void>;
}) {
  const [form] = Form.useForm<EditValues>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Load values only when the modal opens: the page re-polls the bot every 15s (see BotForm)
  const botRef = useRef(bot);
  botRef.current = bot;
  useEffect(() => {
    if (open) {
      const b = botRef.current;
      form.setFieldsValue({
        name: b.name,
        slippage_pct: +(b.slippage_pct * 100).toFixed(4),
        fee_pct: +(b.fee_pct * 100).toFixed(4),
        max_drawdown_pct: +(b.max_drawdown_pct * 100).toFixed(4),
        fractional: !!b.fractional,
      });
      setError(null);
    }
  }, [open, form]);

  const submit = async () => {
    const v = await form.validateFields();
    setSaving(true);
    setError(null);
    try {
      await onSubmit({
        name: v.name.trim(),
        slippage_pct: +(v.slippage_pct / 100).toFixed(6),
        fee_pct: +(v.fee_pct / 100).toFixed(6),
        max_drawdown_pct: +(v.max_drawdown_pct / 100).toFixed(6),
        fractional: v.fractional,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const pctInput = (max: number, step: number) => <InputNumber min={0} max={max} step={step} addonAfter="%" style={{ width: "100%" }} />;
  return (
    <Modal title="Edit rotation bot" open={open} onCancel={onCancel} onOk={submit} okText="Save" okButtonProps={{ loading: saving }} destroyOnClose>
      <Form<EditValues> form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true }]}><Input maxLength={80} /></Form.Item>
        <Row gutter={12}>
          <Col span={8}><Form.Item name="max_drawdown_pct" label="Drawdown breaker">{pctInput(100, 5)}</Form.Item></Col>
          <Col span={8}><Form.Item name="slippage_pct" label="Slippage buffer">{pctInput(5, 0.01)}</Form.Item></Col>
          <Col span={8}><Form.Item name="fee_pct" label="Fee (paper only)">{pctInput(5, 0.01)}</Form.Item></Col>
        </Row>
        <Form.Item name="fractional" valuePropName="checked" extra="From the next rebalance: what it holds now stays as it is until then.">
          <Switch checkedChildren="Fractional shares" unCheckedChildren="Whole shares" />
        </Form.Item>
        <Alert type="info" showIcon message="The universe and the rules are fixed for a bot's life, so its history describes one setup. For other rules, create a new rotation bot." />
        {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
      </Form>
    </Modal>
  );
}
