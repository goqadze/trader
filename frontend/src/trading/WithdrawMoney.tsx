import { Alert, Button, Checkbox, Form, Grid, InputNumber, Modal, Table, Typography } from "antd";
import { type ElementRef, useEffect, useRef, useState } from "react";
import { tradingApi } from "./api";
import { frac, isDip, isRotation, usd } from "./format";
import type { Bot, WithdrawalPlan } from "./types";

const MUTED = "#8b98b5";
// trading-service withdraw.py MIN_LEFT: a bot keeps at least this much to trade with
const MIN_LEFT = 100;

interface Values {
  amount: number;
  confirm_live: boolean;
}

type Sale = WithdrawalPlan["sales"][number];

const shares = (n: number) => (Number.isInteger(n) ? String(n) : String(+n.toFixed(6)));

/** Take money out of a running bot (POST /bots/{id}/withdraw): its cash first, then the same share of each holding,
 *  sold at market. The preview is the service's own plan at the last prices (GET, nothing sent). */
export default function WithdrawMoney({ bot, open, onClose, onDone }: { bot: Bot; open: boolean; onClose: () => void; onDone: (after: Bot) => void }) {
  const [form] = Form.useForm<Values>();
  const amountRef = useRef<ElementRef<typeof InputNumber>>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<WithdrawalPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const amount: number = Form.useWatch("amount", form) ?? 0;
  const phone = Grid.useBreakpoint().sm === false; // the table drops "Holds" and "At" (responsive: ["sm"])

  // The preview follows the amount, a moment after typing stops; a late answer for an older amount is dropped
  useEffect(() => {
    if (!open || !(amount > 0)) {
      setPlan(null);
      setPlanError(null);
      return;
    }
    let stale = false;
    const t = setTimeout(() => {
      tradingApi.withdrawalPlan(bot.id, amount)
        .then((p) => !stale && (setPlan(p), setPlanError(null)))
        .catch((e) => !stale && (setPlan(null), setPlanError(e instanceof Error ? e.message : String(e))));
    }, 300);
    return () => {
      stale = true;
      clearTimeout(t);
    };
  }, [open, amount, bot.id, bot.equity]);

  const most = Math.max(0, Math.round((bot.equity - MIN_LEFT) * 100) / 100);
  const shown = plan && plan.amount === amount ? plan : null;
  // A plan within what the bot can give is worth showing even when it can't go now (market closed, an order working)
  const valid = !!shown && (!shown.problem || shown.amount <= shown.most);
  const sells = (shown?.sales.length ?? 0) > 0;
  const whole = shown?.sales.some((s) => s.whole) ?? false;
  const alpaca = bot.broker.startsWith("alpaca");
  const dip = isDip(bot);
  const after = bot.equity - (amount || 0);

  const submit = async () => {
    const v = await form.validateFields();
    setBusy(true);
    setError(null);
    try {
      onDone(await tradingApi.withdraw(bot.id, { amount: v.amount, confirm_live: !!v.confirm_live }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={`Take money out of ${bot.name}`} open={open} onCancel={onClose} onOk={submit} okText="Take out"
      okButtonProps={{ loading: busy, danger: bot.live, disabled: !(amount > 0) || !!shown?.problem }} width={620} destroyOnClose
      afterOpenChange={(visible) => visible && amountRef.current?.focus()}>
      <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
        Now {usd(bot.equity)} ({usd(bot.cash)} of it cash), from {usd(bot.capital)} put in
        {bot.withdrawn_cash ? `, ${usd(bot.withdrawn_cash)} taken out so far` : ""}. The money comes from its cash first, realized
        profit included, then from selling the same share of each holding at market. What you put in stays the same: the return
        counts what you take out, and buy &amp; hold gives up the same money on the same day.
      </Typography.Paragraph>
      <Form<Values> form={form} layout="vertical" requiredMark={false} preserve={false}>
        <Form.Item label="Amount" required style={{ marginBottom: 4 }}>
          <Form.Item name="amount" noStyle rules={[{ required: true, message: "How much?" }]}>
            <InputNumber ref={amountRef} min={1} max={10_000_000} step={50} addonBefore="$" style={{ width: "100%" }} />
          </Form.Item>
        </Form.Item>
        <div style={{ fontSize: 12, color: MUTED, marginBottom: 12 }}>
          At most {usd(most)}: a bot keeps {usd(MIN_LEFT, 0)} to trade with.
          {most > 0 && <Button type="link" size="small" onClick={() => form.setFieldValue("amount", most)}>Take {usd(most)}</Button>}
          {" "}To take everything out, close its positions and archive it.
        </div>

        {shown && valid && (
          <>
            <Typography.Paragraph style={{ fontSize: 12, marginBottom: 8 }}>
              {sells
                ? <>{usd(shown.from_cash)} from its cash and {usd(shown.amount - shown.from_cash)} from selling about {frac(Math.round(shown.part * 1000) / 1000)} of {holdsManyText(bot)} at market now.</>
                : <>All of it from its cash ({usd(shown.cash)}): nothing is sold.</>}
            </Typography.Paragraph>
            {sells && (
              <>
                <Table<Sale>
                  rowKey="symbol" size="small" pagination={false} dataSource={shown.sales} style={{ marginBottom: 8 }}
                  scroll={{ x: "max-content" }}
                  columns={[
                    { title: "Holding", dataIndex: "symbol" },
                    { title: "Holds", dataIndex: "held", align: "right", responsive: ["sm"], render: (v: number) => shares(v) },
                    { title: "Sells", dataIndex: "qty", align: "right", render: (v: number, r) => `${shares(v)}${v >= r.held ? " (all)" : ""}` },
                    { title: "At", dataIndex: "price", align: "right", responsive: ["sm"], render: (v: number) => usd(v) },
                    { title: "Raises", dataIndex: "raises", align: "right", render: (v: number) => <span style={{ whiteSpace: "nowrap" }}>≈ {usd(v)}</span> },
                  ]}
                  summary={() => (
                    <Table.Summary.Row>
                      <Table.Summary.Cell index={0} colSpan={phone ? 2 : 4}>Sales in all</Table.Summary.Cell>
                      <Table.Summary.Cell index={1} align="right"><span style={{ whiteSpace: "nowrap" }}>≈ {usd(shown.raises)}</span></Table.Summary.Cell>
                    </Table.Summary.Row>
                  )}
                />
                <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
                  Estimates at the last prices: the sales go out at the price then.
                  {whole && ` Whole shares, so they raise a little more: ${usd(shown.left_in_cash)} stays in its cash.`}
                  {" "}If they fill lower, that much less comes out (the bot's cash never goes under zero).
                  {dip && shown.sales.some((x) => x.qty >= x.held) && " A holding sold whole isn't bought again before tomorrow."}
                </Typography.Paragraph>
              </>
            )}
            <Typography.Paragraph style={{ fontSize: 12, color: MUTED }}>
              {dip
                ? `New buys get slots of about ${usd(after / (bot.max_positions || 1))} (1/${bot.max_positions} of the equity); what it holds keeps its stop and target.`
                : isRotation(bot)
                ? `Its next rebalance sizes each pick at 1/${bot.top_n} of the equity.`
                : `Its next buy uses ${frac(bot.position_pct)} of its cash${bot.shares > 0 ? "; the shares left keep their stop and target" : ""}.`}
            </Typography.Paragraph>
          </>
        )}
        {shown?.problem && <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="Can't take that out now" description={shown.problem} />}
        {planError && <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={`No preview: ${planError}`} />}

        {alpaca && (
          <Alert type="info" showIcon style={{ marginBottom: 12 }}
            message="No money leaves Alpaca"
            description={`The bot's share of your Alpaca ${bot.live ? "live" : "paper"} account gets smaller: the money becomes the account's free cash, to withdraw at Alpaca or give to another bot.`} />
        )}
        {bot.live && (
          <>
            <Alert type="error" showIcon style={{ marginBottom: 12 }}
              message="REAL MONEY"
              description={sells ? "The sales are real orders on your Alpaca account, sent as soon as you confirm." : "This bot will trade with less real money."} />
            <Form.Item name="confirm_live" valuePropName="checked" rules={[{ validator: (_, v) => (v ? Promise.resolve() : Promise.reject(new Error("Required for a real-money bot"))) }]}>
              <Checkbox>I understand this changes a real-money bot{sells ? " and sells real shares" : ""}</Checkbox>
            </Form.Item>
          </>
        )}
        {error && <Alert type="error" showIcon message={error} />}
      </Form>
    </Modal>
  );
}

function holdsManyText(bot: Bot): string {
  if (!isDip(bot) && !isRotation(bot)) return "its position";
  return bot.holdings.length > 1 ? "each holding" : "its holding";
}
