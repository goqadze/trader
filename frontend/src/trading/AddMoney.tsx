import { Alert, Checkbox, Form, InputNumber, Modal, Table, Typography } from "antd";
import { type ElementRef, useRef, useState } from "react";
import { tradingApi } from "./api";
import { frac, isDip, isRotation, relative, usd } from "./format";
import type { Bot, Holding } from "./types";

const MUTED = "#8b98b5";
// trading-service rotation.py MIN_TRADE_OF_SLOT: a holding less than this share of a slot short of it isn't topped up
const MIN_TRADE_OF_SLOT = 0.02;

interface Values {
  amount: number;
  top_up: boolean;
  confirm_live: boolean;
}

type Row = Holding & { topUp: number; watched: boolean };

/** Put more money in a running bot (POST /bots/{id}/capital). A dip bot's slots grow with it; with "top up" its next
 *  check that can buy also brings each holding up to a full slot. The preview estimates that at the last prices. */
export default function AddMoney({ bot, open, onClose, onAdded }: { bot: Bot; open: boolean; onClose: () => void; onAdded: () => void }) {
  const [form] = Form.useForm<Values>();
  const amountRef = useRef<ElementRef<typeof InputNumber>>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const amount: number = Form.useWatch("amount", form) ?? 0;
  const topUp: boolean = Form.useWatch("top_up", form) ?? true;

  const dip = isDip(bot);
  const held = dip ? bot.holdings : [];
  const slot = dip && bot.max_positions ? (bot.equity + (amount || 0)) / bot.max_positions : 0;
  const watching = new Set(bot.watchlist.filter((w) => w.status === "watching").map((w) => w.symbol));
  const rows: Row[] = held.map((h) => {
    const short = slot - h.value;
    const watched = watching.has(h.symbol); // off the watchlist or blacklisted: left as it is
    return { ...h, watched, topUp: watched && short >= Math.max(MIN_TRADE_OF_SLOT * slot, 1) ? short : 0 };
  });
  const spend = rows.reduce((t, r) => t + r.topUp, 0);
  const alpaca = bot.broker.startsWith("alpaca");

  const when = bot.status === "paused"
    ? "once you resume it (its first check after that)"
    : `at its next check${bot.next_decision_at ? ` (${relative(bot.next_decision_at)})` : ""}, or now with Check now while the market is open`;

  const submit = async () => {
    const v = await form.validateFields();
    setBusy(true);
    setError(null);
    try {
      await tradingApi.addCapital(bot.id, { amount: v.amount, top_up: held.length > 0 && !!v.top_up, confirm_live: !!v.confirm_live });
      onAdded();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={`Add money to ${bot.name}`} open={open} onCancel={onClose} onOk={submit} okText="Add money"
      okButtonProps={{ loading: busy, danger: bot.live }} width={620} destroyOnClose
      afterOpenChange={(shown) => shown && amountRef.current?.focus()}>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        Now {usd(bot.equity)} ({usd(bot.cash)} of it cash), from {usd(bot.capital)} put in
        {bot.added_cash ? ` (${usd(bot.allocated_cash)} at the start, ${usd(bot.added_cash)} added since)` : ""}. Added money counts as
        money put in, not as profit: the return is measured on all of it, and buy &amp; hold gets the same money on the same day.
      </Typography.Paragraph>
      <Form<Values> form={form} layout="vertical" requiredMark={false} preserve={false} initialValues={{ top_up: true }}>
        <Form.Item name="amount" label="Amount" rules={[{ required: true, message: "How much?" }]}>
          <InputNumber ref={amountRef} min={1} max={10_000_000} step={500} addonBefore="$" style={{ width: "100%" }} />
        </Form.Item>

        {dip && held.length > 0 && (
          <>
            <Form.Item name="top_up" valuePropName="checked" style={{ marginBottom: 8 }}>
              <Checkbox>Also top up what it holds to a full slot</Checkbox>
            </Form.Item>
            {topUp ? (
              <>
                <Typography.Paragraph style={{ fontSize: 12, color: MUTED, marginBottom: 8 }}>
                  A slot becomes {usd(slot)} (1/{bot.max_positions} of the equity). It buys the difference {when}, at the price then,
                  so these amounts are estimates. Each holding's stop moves to {frac(bot.stop_pct)} under its new average buy; its
                  target stays {bot.target_mode === "percent" ? `${frac(bot.rise_pct ?? 0)} above the new average` : "where its fall started"}.
                </Typography.Paragraph>
                <Table<Row>
                  rowKey="symbol" size="small" pagination={false} dataSource={rows} style={{ marginBottom: 12 }}
                  columns={[
                    { title: "Holding", dataIndex: "symbol" },
                    { title: "Worth now", dataIndex: "value", align: "right", render: (v: number) => usd(v) },
                    {
                      title: "Top-up", dataIndex: "topUp", align: "right",
                      render: (v: number, r) => (v ? <span style={{ whiteSpace: "nowrap" }}>≈ {usd(v)}</span> : <span style={{ color: MUTED }}>{r.watched ? "already a full slot" : "not watched: none"}</span>),
                    },
                    { title: "Stop now", dataIndex: "stop_price", align: "right", render: (v: number | null) => usd(v) },
                  ]}
                  summary={() => (
                    <Table.Summary.Row>
                      <Table.Summary.Cell index={0} colSpan={2}>Top-ups in all</Table.Summary.Cell>
                      <Table.Summary.Cell index={2} align="right"><span style={{ whiteSpace: "nowrap" }}>≈ {usd(spend)}</span></Table.Summary.Cell>
                      <Table.Summary.Cell index={3} />
                    </Table.Summary.Row>
                  )}
                />
              </>
            ) : (
              <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
                What it holds stays as it is. New buys get slots of {usd(slot)} (1/{bot.max_positions} of the equity).
              </Typography.Paragraph>
            )}
          </>
        )}
        {dip && held.length === 0 && (
          <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
            Nothing held now. New buys get slots of {usd(slot)} (1/{bot.max_positions} of the equity).
          </Typography.Paragraph>
        )}
        {isRotation(bot) && (
          <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
            Invested at its next rebalance{bot.next_decision_at ? ` (${relative(bot.next_decision_at)})` : ""}: each pick gets 1/{bot.top_n} of the equity, so what it keeps is topped up then.
          </Typography.Paragraph>
        )}
        {!dip && !isRotation(bot) && (
          <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
            Its next buy uses {frac(bot.position_pct)} of its cash{bot.shares > 0 ? "; the position it holds now stays as it is" : ""}.
          </Typography.Paragraph>
        )}

        {alpaca && (
          <Alert type="info" showIcon style={{ marginBottom: 12 }}
            message="This doesn't move any money"
            description={`The bot's cash is its share of your Alpaca ${bot.live ? "live" : "paper"} account. Make sure the account has it: no bot buys beyond the account's buying power.`} />
        )}
        {bot.live && (
          <>
            <Alert type="error" showIcon style={{ marginBottom: 12 }}
              message="More REAL MONEY for this bot"
              description={`Its next buys${topUp && held.length ? " and the top-ups" : ""} are real orders on your Alpaca account, with bigger amounts.`} />
            <Form.Item name="confirm_live" valuePropName="checked" rules={[{ validator: (_, v) => (v ? Promise.resolve() : Promise.reject(new Error("Required for a real-money bot"))) }]}>
              <Checkbox>I understand this bot will trade more real money</Checkbox>
            </Form.Item>
          </>
        )}
        {error && <Alert type="error" showIcon message={error} />}
      </Form>
    </Modal>
  );
}
