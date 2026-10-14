// A journal trade's whole form: new, or editing one. What it asks follows the status: a planned trade is the plan, an
// open one adds the entry, a closed one the exit and the review. A new trade can carry a note: how you feel about it.

import { App as AntApp, AutoComplete, Col, DatePicker, Divider, Form, Grid, Input, InputNumber, Modal, Row, Segmented, Space } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useEffect, useMemo } from "react";
import { pnlColor, usd } from "../trading/format";
import { journalApi } from "./api";
import { MUTED, SETUPS, rText } from "./catalog";
import { FeelingFields, Hint, MistakeSelect, RatingPicker, StagePicker, TagSelect, YesNo, noteFrom, useJournalOptions } from "./parts";
import type { JournalSide, JournalStage, JournalStatus, JournalTradeDetail, JournalTradeFields } from "./types";

interface Values extends Omit<JournalTradeFields, "opened_at" | "closed_at"> {
  opened_at: Dayjs | null;
  closed_at: Dayjs | null;
  note_stage: JournalStage;
  note?: { emotions?: string[]; intensity?: number | null; text?: string };
}

const BLANK: Partial<Values> = {
  status: "planned", side: "long", account: "", setup: "", tags: [], fees: 0, thesis: "", exit_reason: "", mistakes: [],
  review: "", lessons: "", followed_plan: null, grade: null, confidence: null,
};

type PreviewOf = Partial<Pick<JournalTradeFields, "status" | "side" | "entry_price" | "stop_price" | "target_price" | "quantity" | "exit_price" | "fees">>;

/** Risk, reward:risk and (closed) the P&L, worked out as trading-service journal.apply does. */
export function preview(v: PreviewOf) {
  const sign = v.side === "short" ? -1 : 1;
  const { entry_price: entry, stop_price: stop, target_price: target, quantity: qty, exit_price: exit } = v;
  const perShareRisk = entry && stop ? sign * (entry - stop) : null;
  const risk = perShareRisk && perShareRisk > 0 && qty ? perShareRisk * qty : null;
  const rr = perShareRisk && perShareRisk > 0 && target && entry ? Math.abs(target - entry) / perShareRisk : null;
  const pnl = v.status === "closed" && entry && exit && qty ? sign * (exit - entry) * qty - (v.fees || 0) : null;
  return { risk, rr, value: entry && qty ? entry * qty : null, pnl, r: pnl != null && risk ? pnl / risk : null };
}

/** The form's values as the API takes them. */
export function fieldsFrom(v: Values): JournalTradeFields {
  return {
    status: v.status, symbol: v.symbol.trim().toUpperCase(), side: v.side, account: (v.account ?? "").trim(), setup: (v.setup ?? "").trim(),
    tags: v.tags ?? [], quantity: v.quantity ?? null, entry_price: v.entry_price ?? null, stop_price: v.stop_price ?? null,
    target_price: v.target_price ?? null, confidence: v.confidence ?? null, thesis: v.thesis ?? "",
    opened_at: v.opened_at ? v.opened_at.toISOString() : null, closed_at: v.closed_at ? v.closed_at.toISOString() : null,
    exit_price: v.exit_price ?? null, fees: v.fees ?? 0, exit_reason: v.exit_reason ?? "", followed_plan: v.followed_plan ?? null,
    grade: v.grade ?? null, mistakes: v.mistakes ?? [], review: v.review ?? "", lessons: v.lessons ?? "",
  };
}

const money = { min: 0, style: { width: "100%" }, prefix: "$" } as const;
const NOTE_STAGE: Record<JournalStatus, JournalStage> = { planned: "plan", open: "entry", closed: "entry", skipped: "plan" };
const NOTE_LABEL: Record<JournalStage, string> = {
  plan: "How do you feel about this trade?", entry: "How did you feel going in?", during: "How did you feel while it ran?",
  exit: "How did you feel getting out?", review: "How do you feel about it now?", checkin: "",
};

export default function TradeForm({ open, trade, onClose, onSaved }: {
  open: boolean;
  trade?: JournalTradeDetail | null; // editing this one; none: a new trade
  onClose: () => void;
  onSaved: (t: JournalTradeDetail) => void;
}) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<Values>();
  const { options, refresh } = useJournalOptions();
  const mobile = Grid.useBreakpoint().sm === false;
  const values = Form.useWatch([], form) as Partial<Values> | undefined;
  const status: JournalStatus = values?.status ?? trade?.status ?? "planned";
  const side: JournalSide = values?.side ?? "long";
  const p = useMemo(() => preview(values ?? {}), [values]);
  const editing = !!trade;

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    form.setFieldsValue(trade
      ? { ...trade, opened_at: trade.opened_at ? dayjs(trade.opened_at) : null, closed_at: trade.closed_at ? dayjs(trade.closed_at) : null }
      : { ...BLANK, note_stage: "plan" });
  }, [open, trade, form]);

  // Entering or closing it: the time defaults to now
  const onStatus = (s: JournalStatus) => {
    const now = dayjs();
    if ((s === "open" || s === "closed") && !form.getFieldValue("opened_at")) form.setFieldValue("opened_at", now);
    if (s === "closed" && !form.getFieldValue("closed_at")) form.setFieldValue("closed_at", now);
    if (!editing) form.setFieldValue("note_stage", NOTE_STAGE[s]);
  };

  const save = async () => {
    const v = await form.validateFields();
    const fields = fieldsFrom({ ...form.getFieldsValue(true), ...v });
    const note = editing ? null : noteFrom(form.getFieldValue("note"));
    // Logging a trade afterwards: the feeling is dated at its step (going in: the entry; getting out: the exit), so the
    // timeline reads in order; a plan or review note is dated now
    const stage: JournalStage = form.getFieldValue("note_stage");
    const at = stage === "exit" ? fields.closed_at : stage === "entry" || stage === "during" ? fields.opened_at : null;
    try {
      const saved = editing
        ? await journalApi.updateTrade(trade.id, fields)
        : await journalApi.createTrade({ ...fields, note: note ? { ...note, stage, ...(at ? { at } : {}) } : null });
      message.success(editing ? "Saved" : `${saved.symbol} is in your journal`);
      void refresh();
      onSaved(saved);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e), 8);
    }
  };

  const needs = (statuses: JournalStatus[], what: string) => ({
    required: statuses.includes(status), message: `${status === "open" ? "An open" : "A closed"} trade needs its ${what}`,
  });
  const col = mobile ? 12 : 8;
  return (
    <Modal
      open={open}
      title={editing ? `Edit ${trade.symbol}` : "New journal trade"}
      onCancel={onClose}
      onOk={save}
      okText={editing ? "Save" : "Add to journal"}
      width={780}
      style={{ top: 16 }}
      destroyOnHidden
    >
      <Form<Values> form={form} layout="vertical" requiredMark={false} size={mobile ? "middle" : undefined}>
        <Form.Item name="status" label="Where is this trade?" style={{ marginBottom: 8 }}>
          <Segmented
            onChange={(s) => onStatus(s as JournalStatus)}
            options={[
              { value: "planned", label: "Planning it" },
              { value: "open", label: "In it now" },
              { value: "closed", label: "Closed" },
              ...(editing ? [{ value: "skipped", label: "Skipped" }] : []),
            ]}
          />
        </Form.Item>
        <Row gutter={12}>
          <Col xs={12} sm={6}>
            <Form.Item name="symbol" label="Symbol" rules={[{ required: true, message: "Which symbol?" }, { pattern: /^\s*[A-Za-z0-9^][A-Za-z0-9.\-/=^]{0,23}\s*$/, message: "Like AAPL, BRK.B, BTC-USD" }]}>
              <AutoComplete options={options.symbols.map((s) => ({ value: s }))} filterOption={(i, o) => !!o?.value.toUpperCase().startsWith(i.toUpperCase())}>
                <Input placeholder="AAPL" style={{ textTransform: "uppercase" }} maxLength={24} />
              </AutoComplete>
            </Form.Item>
          </Col>
          <Col xs={12} sm={6}>
            <Form.Item name="side" label="Side">
              <Segmented options={[{ value: "long", label: "Long" }, { value: "short", label: "Short" }]} />
            </Form.Item>
          </Col>
          <Col xs={12} sm={6}>
            <Form.Item name="setup" label="Setup" tooltip="The pattern or strategy: the statistics compare your setups">
              <AutoComplete
                options={[...new Set([...options.setups, ...SETUPS])].map((s) => ({ value: s }))}
                filterOption={(i, o) => !!o?.value.toLowerCase().includes(i.toLowerCase())}
              >
                <Input placeholder="Breakout" maxLength={60} />
              </AutoComplete>
            </Form.Item>
          </Col>
          <Col xs={12} sm={6}>
            <Form.Item name="account" label="Account" tooltip="Where you traded it">
              <AutoComplete options={options.accounts.map((s) => ({ value: s }))}>
                <Input placeholder="Alpaca live" maxLength={60} />
              </AutoComplete>
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="tags" label="Tags" style={{ marginBottom: 8 }}>
          <TagSelect used={options.tags} />
        </Form.Item>

        <Divider orientation="left" plain style={{ margin: "8px 0" }}>The plan</Divider>
        <Row gutter={12}>
          <Col xs={col} sm={6}>
            <Form.Item name="entry_price" label={status === "planned" ? "Entry (planned)" : "Entry price"} rules={[needs(["open", "closed"], "entry price")]}>
              <InputNumber {...money} />
            </Form.Item>
          </Col>
          <Col xs={col} sm={6}>
            <Form.Item
              name="stop_price"
              label="Stop-loss"
              tooltip="Where you're wrong. Its distance from the entry is one R: your risk"
              dependencies={["entry_price", "side"]}
              rules={[{ validator: (_, stop) => {
                const entry = form.getFieldValue("entry_price");
                return !stop || !entry || (side === "long" ? stop < entry : stop > entry)
                  ? Promise.resolve() : Promise.reject(new Error(`A ${side} trade's stop is ${side === "long" ? "below" : "above"} the entry`));
              } }]}
            >
              <InputNumber {...money} />
            </Form.Item>
          </Col>
          <Col xs={col} sm={6}>
            <Form.Item
              name="target_price"
              label="Target"
              dependencies={["entry_price", "side"]}
              rules={[{ validator: (_, target) => {
                const entry = form.getFieldValue("entry_price");
                return !target || !entry || (side === "long" ? target > entry : target < entry)
                  ? Promise.resolve() : Promise.reject(new Error(`A ${side} trade's target is ${side === "long" ? "above" : "below"} the entry`));
              } }]}
            >
              <InputNumber {...money} />
            </Form.Item>
          </Col>
          <Col xs={col} sm={6}>
            <Form.Item name="quantity" label="Shares / units" rules={[needs(["open", "closed"], "quantity")]}>
              <InputNumber min={0} style={{ width: "100%" }} />
            </Form.Item>
          </Col>
        </Row>
        <Hint>
          <Space size={16} wrap>
            <span>Position {p.value != null ? usd(p.value) : "—"}</span>
            <span>Risk (1R) {p.risk != null ? usd(p.risk) : "— set a stop"}</span>
            <span>Reward : risk {p.rr != null ? `${p.rr.toFixed(2)} : 1` : "—"}</span>
          </Space>
        </Hint>
        <Form.Item name="confidence" label="Confidence before entering" style={{ marginBottom: 8 }}>
          <RatingPicker words="confidence" />
        </Form.Item>
        <Form.Item name="thesis" label="Why this trade?" tooltip="Your reason, and what would prove it wrong">
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 10 }} maxLength={10000} placeholder="e.g. Pulled back to the 50-day on light volume; wrong if it closes under $95" />
        </Form.Item>

        {status !== "planned" && status !== "skipped" && (
          <>
            <Divider orientation="left" plain style={{ margin: "8px 0" }}>What happened</Divider>
            <Row gutter={12}>
              <Col xs={24} sm={12}>
                <Form.Item name="opened_at" label="Entered at" rules={[needs(["open", "closed"], "entry time")]}>
                  <DatePicker showTime={{ format: "HH:mm" }} format="YYYY-MM-DD HH:mm" style={{ width: "100%" }} />
                </Form.Item>
              </Col>
              {status === "closed" && (
                <Col xs={24} sm={12}>
                  <Form.Item
                    name="closed_at"
                    label="Exited at"
                    dependencies={["opened_at"]}
                    rules={[needs(["closed"], "exit time"), { validator: (_, at: Dayjs | null) => {
                      const from: Dayjs | null = form.getFieldValue("opened_at");
                      return !at || !from || !at.isBefore(from) ? Promise.resolve() : Promise.reject(new Error("The exit can't be before the entry"));
                    } }]}
                  >
                    <DatePicker showTime={{ format: "HH:mm" }} format="YYYY-MM-DD HH:mm" style={{ width: "100%" }} />
                  </Form.Item>
                </Col>
              )}
            </Row>
            {status === "closed" && (
              <>
                <Row gutter={12}>
                  <Col xs={12} sm={8}>
                    <Form.Item name="exit_price" label="Exit price" rules={[needs(["closed"], "exit price")]}>
                      <InputNumber {...money} />
                    </Form.Item>
                  </Col>
                  <Col xs={12} sm={8}>
                    <Form.Item name="fees" label="Fees (in + out)">
                      <InputNumber {...money} />
                    </Form.Item>
                  </Col>
                  <Col xs={24} sm={8}>
                    <Form.Item label="Result">
                      <span style={{ fontSize: 16, fontWeight: 600, color: pnlColor(p.pnl) }}>
                        {p.pnl != null ? usd(p.pnl) : "—"}
                      </span>
                      <span style={{ color: MUTED, marginLeft: 8 }}>{p.r != null ? rText(p.r) : ""}</span>
                    </Form.Item>
                  </Col>
                </Row>
                <Form.Item name="exit_reason" label="Why did you exit?">
                  <Input maxLength={2000} placeholder="Target hit, stop hit, changed my mind, end of day ..." />
                </Form.Item>
              </>
            )}
          </>
        )}

        {editing && status === "closed" && (
          <>
            <Divider orientation="left" plain style={{ margin: "8px 0" }}>Review</Divider>
            <Row gutter={12}>
              <Col xs={24} sm={12}>
                <Form.Item name="followed_plan" label="Did you follow your plan?"><YesNo /></Form.Item>
              </Col>
              <Col xs={24} sm={12}>
                <Form.Item name="grade" label="How well did you trade it?" tooltip="Your execution, whatever the P&L"><RatingPicker words="grade" /></Form.Item>
              </Col>
            </Row>
            <Form.Item name="mistakes" label="Mistakes"><MistakeSelect used={options.mistakes} /></Form.Item>
            <Form.Item name="review" label="What went well, what would you do differently?">
              <Input.TextArea autoSize={{ minRows: 2, maxRows: 10 }} maxLength={10000} />
            </Form.Item>
            <Form.Item name="lessons" label="Lesson">
              <Input.TextArea autoSize={{ minRows: 1, maxRows: 6 }} maxLength={10000} />
            </Form.Item>
          </>
        )}

        {!editing && (
          <>
            <Divider orientation="left" plain style={{ margin: "8px 0" }}>Your feelings</Divider>
            <Form.Item name="note_stage" label="About" style={{ marginBottom: 8 }}>
              <StagePicker stages={["plan", "entry", "during", "exit", "review"]} />
            </Form.Item>
            <FeelingFields extraEmotions={options.emotions} emotionsLabel={NOTE_LABEL[(values?.note_stage ?? "plan") as JournalStage]} />
            <Hint>Optional. You can add a note at every step later: entering, while it runs, exiting, reviewing.</Hint>
          </>
        )}
      </Form>
    </Modal>
  );
}
