// A real account's money from what it started with (trading-service ledger.py): the overview's cards on its tab, and the
// Money history popup with the sum that checks the bots' records against the account and every move of its money.

import { DeleteOutlined } from "@ant-design/icons";
import { App as AntApp, Alert, Button, Card, Col, DatePicker, Form, Grid, Input, InputNumber, Modal, Segmented, Space, Statistic, Table, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import { useState } from "react";
import { Link } from "react-router-dom";
import { tradingApi } from "./api";
import { ArchivedLine, Money, day } from "./ArchivedHistory";
import { useConfirm } from "./confirm";
import { nyTime, pct, pnlColor, usd } from "./format";
import type { Ledger, LedgerMove, MoneyIn } from "./types";

const MUTED = "#8b98b5";
const small = { color: MUTED, fontSize: 12 };
const link = { padding: 0, height: "auto", fontSize: 12 };

const ACCOUNT_LINE: Partial<Record<LedgerMove["kind"], string>> = {
  start: "The account started with this",
  deposit: "Real deposit into the account",
  withdrawal: "Real withdrawal from the account",
  fee: "Alpaca's fee no bot paid",
};
const BOT_LINE: Partial<Record<LedgerMove["kind"], string>> = {
  new_bot: "Started",
  add: "Added to",
  take_out: "Taken out of",
  archived: "Archived, its cash came back:",
};

/** Under the day, what moved: the account's own money, or a bot with a link to it. */
function What({ m }: { m: LedgerMove }) {
  return (
    <>
      {ACCOUNT_LINE[m.kind] ?? (
        <>
          {BOT_LINE[m.kind]} <Link to={`/trading/${m.bot_id}`}>#{m.bot_id} {m.bot_name}</Link>
        </>
      )}
      {m.note && <div style={small}>{m.note}</div>}
    </>
  );
}

/** Writes down what the account started with, or a real deposit or withdrawal at the broker. */
function MoneyForm({ ledger, onSaved }: { ledger: Ledger; onSaved: (l: Ledger) => void }) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<{ amount: number; day: Dayjs | null; note?: string }>();
  const [kind, setKind] = useState<"deposit" | "withdrawal">("deposit");
  const [saving, setSaving] = useState(false);
  const starting = ledger.start == null;

  const save = async (v: { amount: number; day: Dayjs | null; note?: string }) => {
    const body: MoneyIn = { kind: starting ? "start" : kind, amount: v.amount, note: v.note ?? "" };
    if (v.day) body.day = v.day.format("YYYY-MM-DD");
    setSaving(true);
    try {
      onSaved(await tradingApi.recordMoney(ledger.broker, body));
      form.resetFields();
      message.success("Written down");
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Form form={form} layout="vertical" size="small" onFinish={save} requiredMark={false}
      initialValues={{ day: starting && ledger.first_bot_day ? dayjs(ledger.first_bot_day) : dayjs() }}>
      {!starting && (
        <Form.Item label="What happened at the broker" style={{ marginBottom: 8 }}>
          <Segmented value={kind} onChange={(v) => setKind(v as typeof kind)}
            options={[{ label: "Deposit", value: "deposit" }, { label: "Withdrawal", value: "withdrawal" }]} />
        </Form.Item>
      )}
      <Space wrap align="start" size={8}>
        <Form.Item name="amount" label={starting ? "Started with" : "Amount"} style={{ marginBottom: 8 }}
          rules={[{ required: true, message: "How much?" }]}>
          <InputNumber min={0.01} max={10_000_000} step={1} prefix="$" style={{ width: 130 }} />
        </Form.Item>
        <Form.Item name="day" label="Day" style={{ marginBottom: 8 }}>
          <DatePicker allowClear={false} disabledDate={(d) => d.isAfter(dayjs(), "day")} style={{ width: 130 }} />
        </Form.Item>
        <Form.Item name="note" label="Note" style={{ marginBottom: 8 }}>
          <Input maxLength={200} placeholder="Optional" style={{ width: 160 }} />
        </Form.Item>
      </Space>
      <Button type="primary" htmlType="submit" size="small" loading={saving}>Write it down</Button>
      <div style={{ ...small, marginTop: 8 }}>Only a record: no money moves and Alpaca isn't asked.</div>
    </Form>
  );
}

/** One line of the sum. */
const Line = ({ label, v, signed, strong, tip }: { label: string; v: number | null; signed?: boolean; strong?: boolean; tip?: string }) => {
  const shown = v == null ? "unknown" : signed ? <Money v={v} /> : usd(v);
  const text = strong ? <b>{label}</b> : label;
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "2px 0", borderTop: strong ? "1px solid #303030" : undefined }}>
      <span style={strong ? undefined : { color: MUTED }}>{tip ? <Tooltip title={tip}>{text}</Tooltip> : text}</span>
      <span style={{ fontWeight: strong ? 600 : undefined, whiteSpace: "nowrap" }}>{shown}</span>
    </div>
  );
};

/** The sum: what should be in the account by the records, what is, and the difference between them. */
function TheSum({ l }: { l: Ledger }) {
  return (
    <Card size="small" style={{ marginBottom: 12 }}>
      <Line label="Started with" v={l.start} />
      {l.deposits > 0 && <Line label="Real deposits" v={l.deposits} signed />}
      {l.withdrawals > 0 && <Line label="Real withdrawals" v={-l.withdrawals} signed />}
      <Line label="Running bots' gains and losses" v={l.running_result} signed />
      <Line label="Archived bots' gains and losses" v={l.archived_result} signed />
      {l.fees_no_bot > 0 && <Line label="Fees no bot paid" v={-l.fees_no_bot} signed />}
      <Line label="Should be in the account" v={l.expected} strong />
      <div style={{ height: 8 }} />
      <Line label="Bots' equity" v={l.bots_equity} />
      <Line label="Free cash" v={l.free_cash} />
      <Line label="Is in the account" v={l.equity} strong />
      {l.difference != null && (
        <Line label="Difference" v={l.difference} signed
          tip="What the records can't explain: a deposit not written down here, a dividend, a fractional fill's rounding. For a minute around a fill it can also be the account's read (up to 30 seconds old) and the bots' records being apart." />
      )}
      {l.error && <div style={{ ...small, marginTop: 6 }}>The account can't be read now: {l.error}</div>}
      <div style={{ ...small, marginTop: 8 }}>
        Gains and losses are after Alpaca's fees ({usd(l.fees)} so far). Between bots {usd(l.added)} was added and {usd(l.taken_out)} taken
        out: those only pass through free cash, so they change no line above.
      </div>
    </Card>
  );
}

/** The Money history popup: the sum, the form, and every move of the account's money, the newest first: right under the
 * sum, where its free cash can be compared, and on the first page however long the history gets. */
export function MoneyHistory({ ledger, open, onClose, onChanged }: { ledger: Ledger; open: boolean; onClose: () => void; onChanged: (l: Ledger) => void }) {
  const phone = Grid.useBreakpoint().sm === false;
  const { message } = AntApp.useApp();
  const confirm = useConfirm();
  const [adding, setAdding] = useState(false);

  const forget = (m: LedgerMove) => confirm({
    title: "Delete this line?",
    content: `${ACCOUNT_LINE[m.kind]}: ${usd(Math.abs(m.amount))} on ${day(m.at)}. Only the record goes: no money moves.`,
    okText: "Delete",
    danger: true,
    onOk: async () => {
      try {
        onChanged(await tradingApi.deleteMoney(ledger.broker, m.id as number));
      } catch (e) {
        message.error(e instanceof Error ? e.message : String(e));
      }
    },
  });

  const when = (m: LedgerMove) => <Tooltip title={nyTime(m.at)}>{day(m.at)}</Tooltip>;
  const remove = (m: LedgerMove) => m.id != null && (
    <Tooltip title="Delete this line (a mistake)">
      <Button type="text" size="small" icon={<DeleteOutlined />} aria-label="Delete this line" onClick={() => forget(m)} />
    </Tooltip>
  );
  const amount = { title: <Tooltip title="How much free cash this line moved">Free cash</Tooltip>, dataIndex: "amount", align: "right", render: (v: number) => <Money v={v} /> } as const;
  const after = { title: <Tooltip title="Free cash by the records after this line. The newest one against the account's free cash is the difference in the sum above.">Then</Tooltip>, dataIndex: "free_after", align: "right", render: (v: number) => usd(v) } as const;
  // A phone has room for the two amounts only: the day goes under what happened
  const columns: ColumnsType<LedgerMove> = phone ? [
    { title: "Move", key: "what", render: (_, m) => <><What m={m} /><div style={small}>{when(m)} {remove(m)}</div></> },
    amount,
    after,
  ] : [
    { title: "Day", key: "at", width: 80, render: (_, m) => when(m) },
    { title: "Move", key: "what", render: (_, m) => <What m={m} /> },
    { ...amount, width: 100 },
    { ...after, width: 100 },
    { key: "remove", width: 40, render: (_, m) => remove(m) },
  ];

  return (
    <Modal title={phone ? "Money history" : `Money history · ${ledger.label}`} open={open} onCancel={onClose} footer={null} width={phone ? undefined : 820} destroyOnHidden>
      {ledger.start == null ? (
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message="Write down what this account started with"
          description="The account's capital and total loss are measured against it. It stays in the database, not in the code." />
      ) : (
        <TheSum l={ledger} />
      )}
      {ledger.start == null ? (
        <Card size="small" style={{ marginBottom: 12 }}><MoneyForm ledger={ledger} onSaved={onChanged} /></Card>
      ) : adding ? (
        <Card size="small" style={{ marginBottom: 12 }}><MoneyForm ledger={ledger} onSaved={(l) => { onChanged(l); setAdding(false); }} /></Card>
      ) : (
        <Button size="small" style={{ marginBottom: 12 }} onClick={() => setAdding(true)}>Write down a real deposit or withdrawal</Button>
      )}
      <Table<LedgerMove>
        rowKey={(m) => `${m.kind}-${m.id ?? m.bot_id}-${m.at}`}
        size="small"
        dataSource={[...ledger.moves].reverse()}
        columns={columns}
        pagination={{ pageSize: 50, hideOnSinglePage: true, showSizeChanger: false }}
      />
    </Modal>
  );
}

/** The overview's Capital, Current equity and Total P&L on a real account's tab, once its start is written down: from
 * that start, not from what the running bots hold, so moving money between bots changes none of them. */
export function AccountCards({ ledger, archived, onChanged }: { ledger: Ledger; archived: number; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [fresh, setFresh] = useState<Ledger | null>(null);
  const l = open && fresh ? fresh : ledger; // a line just written down shows at once, not at the next poll
  const own = l.deposits > 0 || l.withdrawals > 0;
  const off = l.difference != null && Math.abs(l.difference) >= 0.005;
  return (
    <>
      <Col xs={12} md={6}>
        <Card size="small">
          <Statistic title={own ? "Capital" : "Starting capital"} value={usd(l.capital)} />
          {own && (
            <div style={small}>
              {[`${usd(l.start, 0)} start`, l.deposits > 0 && `+ ${usd(l.deposits, 0)} deposited`, l.withdrawals > 0 && `− ${usd(l.withdrawals, 0)} withdrawn`].filter(Boolean).join(" ")}
            </div>
          )}
          {(l.added > 0 || l.taken_out > 0) && (
            <Tooltip title="Moved between bots through free cash: no new money, so it's not in the capital">
              <div style={small}>Between bots: {usd(l.added, 0)} added · {usd(l.taken_out, 0)} taken out</div>
            </Tooltip>
          )}
          <Button type="link" size="small" style={link} onClick={() => { setFresh(null); setOpen(true); }}>Money history</Button>
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card size="small">
          <Statistic title="Current equity" value={usd(l.equity ?? l.bots_equity)} />
          <div style={small}>
            {l.free_cash == null ? "Bots only: the account can't be read" : `Bots ${usd(l.bots_equity)} + free cash ${usd(l.free_cash)}`}
          </div>
          {off && (
            <Tooltip title="The records say a little more or less than the account has: Money history shows the sum.">
              <div style={small}>{usd(Math.abs(l.difference as number))} {(l.difference as number) < 0 ? "under" : "over"} the records</div>
            </Tooltip>
          )}
        </Card>
      </Col>
      <Col xs={12} md={6}>
        <Card size="small">
          <Statistic title="Total P&L" value={usd(l.result)} valueStyle={{ color: pnlColor(l.result) }} />
          <div style={small}>
            {pct(l.result_pct)} of {usd(l.capital, 0)}
            {l.fees > 0 && <Tooltip title="Alpaca's fees, already counted"> · after {usd(l.fees)} fees</Tooltip>}
          </div>
          <div style={small}>Running bots: <Money v={l.running_result} /></div>
          <ArchivedLine broker={l.broker} label={l.label} count={archived} result={l.archived_result} runningPnl={l.running_result} included />
        </Card>
      </Col>
      <MoneyHistory ledger={l} open={open} onClose={() => setOpen(false)} onChanged={(x) => { setFresh(x); onChanged(); }} />
    </>
  );
}

/** Under the Capital card of a real account whose start isn't written down yet. */
export function SetStart({ ledger, onChanged }: { ledger: Ledger; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <Button type="link" size="small" style={link} onClick={() => setOpen(true)}>Write down what the account started with</Button>
      <MoneyHistory ledger={ledger} open={open} onClose={() => setOpen(false)} onChanged={onChanged} />
    </div>
  );
}
