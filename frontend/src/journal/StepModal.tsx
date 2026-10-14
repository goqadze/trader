// The steps of a journal trade, each a small dialog that also asks how you feel at that moment (saved as a note of
// that stage): entering it, closing it, reviewing it, skipping it, or just a note while it runs. And the check-in:
// how you feel right now, about no trade in particular (or about one you're in).

import { App as AntApp, Col, DatePicker, Form, Input, InputNumber, Modal, Row, Select, Space } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useEffect, useState } from "react";
import { pnlColor, usd } from "../trading/format";
import { journalApi } from "./api";
import { MUTED, rText } from "./catalog";
import { FeelingFields, Hint, MistakeSelect, RatingPicker, StagePicker, YesNo, noteFrom, useJournalOptions } from "./parts";
import { preview } from "./TradeForm";
import type { JournalNote, JournalNoteIn, JournalStage, JournalTrade, JournalTradeDetail, JournalTradeUpdate } from "./types";

export type Step = "enter" | "close" | "review" | "skip" | "note";

const TITLE: Record<Step, string> = { enter: "Enter the trade", close: "Close the trade", review: "Review the trade", skip: "Skip this trade", note: "Add a note" };
const OK: Record<Step, string> = { enter: "I'm in", close: "Closed", review: "Save review", skip: "Skip it", note: "Save note" };
const FEEL: Record<Step, string> = {
  enter: "How do you feel going in?", close: "How do you feel getting out?", review: "How do you feel about it now?",
  skip: "How do you feel about passing on it?", note: "How do you feel?",
};
const STEP_STAGE: Record<Exclude<Step, "note">, JournalStage> = { enter: "entry", close: "exit", review: "review", skip: "plan" };
const TRADE_STAGES: JournalStage[] = ["plan", "entry", "during", "exit", "review"];
/** A note's stage when you add one: what fits where the trade is. */
export const noteStage = (t: Pick<JournalTrade, "status">): JournalStage => (t.status === "planned" ? "plan" : t.status === "open" ? "during" : "review");

const money = { min: 0, style: { width: "100%" }, prefix: "$" } as const;
const timeInput = <DatePicker showTime={{ format: "HH:mm" }} format="YYYY-MM-DD HH:mm" style={{ width: "100%" }} allowClear={false} />;
const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

interface Values {
  entry_price?: number | null;
  quantity?: number | null;
  exit_price?: number | null;
  fees?: number | null;
  exit_reason?: string;
  at?: Dayjs;
  followed_plan?: boolean | null;
  grade?: number | null;
  mistakes?: string[];
  review?: string;
  lessons?: string;
  stage?: JournalStage;
  note?: { emotions?: string[]; intensity?: number | null; text?: string };
}

export function StepModal({ step, trade, note, onClose, onSaved }: {
  step: Step | null;
  trade: JournalTradeDetail;
  note?: JournalNote | null; // step "note": editing this one
  onClose: () => void;
  onSaved: (t: JournalTradeDetail) => void;
}) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<Values>();
  const { options, refresh } = useJournalOptions();
  const [saving, setSaving] = useState(false);
  const v = Form.useWatch([], form) as Values | undefined;

  useEffect(() => {
    if (!step) return;
    form.resetFields();
    const now = dayjs();
    form.setFieldsValue({
      entry_price: trade.entry_price, quantity: trade.quantity, exit_price: trade.exit_price, fees: trade.fees, exit_reason: trade.exit_reason,
      followed_plan: trade.followed_plan, grade: trade.grade, mistakes: trade.mistakes, review: trade.review, lessons: trade.lessons,
      at: note ? dayjs(note.at) : step === "close" && trade.closed_at ? dayjs(trade.closed_at) : step === "enter" && trade.opened_at ? dayjs(trade.opened_at) : now,
      stage: note?.stage ?? noteStage(trade),
      note: note ? { emotions: note.emotions, intensity: note.intensity, text: note.text } : { emotions: [], intensity: null, text: "" },
    });
  }, [step, trade, note, form]);

  if (!step) return null;
  const close = step === "close";
  const p = preview({ ...trade, status: "closed", exit_price: v?.exit_price ?? null, fees: v?.fees ?? 0 });

  const save = async () => {
    const f = await form.validateFields();
    const feeling = noteFrom(f.note);
    setSaving(true);
    try {
      let saved: JournalTradeDetail;
      if (step === "note") {
        if (!feeling) throw new Error("Pick a feeling or write something");
        const body: JournalNoteIn = { ...feeling, stage: f.stage!, at: f.at!.toISOString() };
        if (note) await journalApi.updateNote(note.id, body);
        else await journalApi.addNote({ ...body, trade_id: trade.id });
        saved = await journalApi.trade(trade.id);
      } else {
        const at = (f.at ?? dayjs()).toISOString(); // a review or a skip has no time field: it's written now
        const body: JournalTradeUpdate =
          step === "enter" ? { status: "open", entry_price: f.entry_price, quantity: f.quantity, opened_at: at, fees: f.fees ?? 0 }
          : step === "close" ? { status: "closed", exit_price: f.exit_price, closed_at: at, fees: f.fees ?? 0, exit_reason: (f.exit_reason ?? "").trim() }
          : step === "review" ? { followed_plan: f.followed_plan ?? null, grade: f.grade ?? null, mistakes: f.mistakes ?? [], review: f.review ?? "", lessons: f.lessons ?? "" }
          : { status: "skipped" };
        saved = await journalApi.updateTrade(trade.id, { ...body, note: feeling ? { ...feeling, stage: STEP_STAGE[step], at } : null });
      }
      void refresh();
      onSaved(saved);
    } catch (e) {
      message.error(errorText(e), 8);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open title={`${note ? "Edit the note" : TITLE[step]} · ${trade.symbol}`} onCancel={onClose} onOk={save} okText={note ? "Save" : OK[step]}
           confirmLoading={saving} width={640} style={{ top: 24 }} destroyOnHidden>
      <Form<Values> form={form} layout="vertical" requiredMark={false}>
        {step === "enter" && (
          <Row gutter={12}>
            <Col xs={12} sm={6}><Form.Item name="entry_price" label="Fill price" rules={[{ required: true, message: "At what price?" }]}><InputNumber {...money} /></Form.Item></Col>
            <Col xs={12} sm={6}><Form.Item name="quantity" label="Shares / units" rules={[{ required: true, message: "How many?" }]}><InputNumber min={0} style={{ width: "100%" }} /></Form.Item></Col>
            <Col xs={24} sm={12}><Form.Item name="at" label="When" rules={[{ required: true }]}>{timeInput}</Form.Item></Col>
          </Row>
        )}
        {close && (
          <>
            <Row gutter={12}>
              <Col xs={12} sm={6}><Form.Item name="exit_price" label="Exit price" rules={[{ required: true, message: "At what price?" }]}><InputNumber {...money} /></Form.Item></Col>
              <Col xs={12} sm={6}><Form.Item name="fees" label="Fees (in + out)"><InputNumber {...money} /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="at" label="When" rules={[{ required: true }]}>{timeInput}</Form.Item></Col>
            </Row>
            <Hint>
              Result: <b style={{ color: pnlColor(p.pnl) }}>{p.pnl != null ? usd(p.pnl) : "—"}</b>
              {p.r != null && <span style={{ marginLeft: 8 }}>{rText(p.r)}</span>}
            </Hint>
            <Form.Item name="exit_reason" label="Why now?"><Input maxLength={2000} placeholder="Target hit, stop hit, changed my mind ..." /></Form.Item>
          </>
        )}
        {step === "review" && (
          <>
            <Row gutter={12}>
              <Col xs={24} sm={12}><Form.Item name="followed_plan" label="Did you follow your plan?"><YesNo /></Form.Item></Col>
              <Col xs={24} sm={12}><Form.Item name="grade" label="How well did you trade it?" tooltip="Your execution, whatever the P&L"><RatingPicker words="grade" /></Form.Item></Col>
            </Row>
            <Form.Item name="mistakes" label="Mistakes"><MistakeSelect used={options.mistakes} /></Form.Item>
            <Form.Item name="review" label="What went well, what would you do differently?"><Input.TextArea autoSize={{ minRows: 2, maxRows: 10 }} maxLength={10000} /></Form.Item>
            <Form.Item name="lessons" label="Lesson for next time"><Input.TextArea autoSize={{ minRows: 1, maxRows: 6 }} maxLength={10000} /></Form.Item>
          </>
        )}
        {step === "skip" && <Hint>It stays in your journal as skipped: the trades you pass on are worth a look later too.</Hint>}
        {step === "note" && (
          <Row gutter={12}>
            <Col xs={24} sm={14}>
              <Form.Item name="stage" label="Stage">
                <StagePicker stages={TRADE_STAGES} />
              </Form.Item>
            </Col>
            <Col xs={24} sm={10}><Form.Item name="at" label="When" rules={[{ required: true }]}>{timeInput}</Form.Item></Col>
          </Row>
        )}
        <FeelingFields extraEmotions={options.emotions} emotionsLabel={FEEL[step]}
                       textLabel={step === "skip" ? "Why are you passing on it?" : "What's on your mind?"} />
        {step !== "note" && <Hint>The feelings are optional; they're saved as a note of this step.</Hint>}
      </Form>
    </Modal>
  );
}

/** How you feel right now: a check-in for the day, or a note on a trade you're in. Also edits a check-in. */
export function CheckInModal({ open, note, at, openTrades = [], onClose, onSaved }: {
  open: boolean;
  note?: JournalNote | null;
  at?: Dayjs; // the moment it's about (a past day's page); default now
  openTrades?: JournalTrade[];
  onClose: () => void;
  onSaved: (n: JournalNote) => void;
}) {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<{ at: Dayjs; trade_id: number | null; note: Values["note"] }>();
  const { options, refresh } = useJournalOptions();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    form.setFieldsValue({
      at: note ? dayjs(note.at) : at ?? dayjs(), trade_id: null,
      note: note ? { emotions: note.emotions, intensity: note.intensity, text: note.text } : { emotions: [], intensity: null, text: "" },
    });
  }, [open, note, at, form]);

  const save = async () => {
    const f = await form.validateFields();
    const feeling = noteFrom(f.note);
    if (!feeling) {
      message.warning("Pick a feeling or write something");
      return;
    }
    setSaving(true);
    try {
      const body = { ...feeling, at: f.at.toISOString() };
      const saved = note
        ? await journalApi.updateNote(note.id, body)
        : await journalApi.addNote(f.trade_id ? { ...body, stage: "during", trade_id: f.trade_id } : { ...body, stage: "checkin" });
      message.success("Checked in");
      void refresh();
      onSaved(saved);
    } catch (e) {
      message.error(errorText(e), 8);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} title={note ? "Edit the check-in" : "💬 Check in: how are you doing?"} onCancel={onClose} onOk={save}
           okText="Save" confirmLoading={saving} width={600} style={{ top: 24 }} destroyOnHidden>
      <Form form={form} layout="vertical" requiredMark={false}>
        <Row gutter={12}>
          <Col xs={24} sm={12}><Form.Item name="at" label="When" rules={[{ required: true }]}>{timeInput}</Form.Item></Col>
          {!note && openTrades.length > 0 && (
            <Col xs={24} sm={12}>
              <Form.Item name="trade_id" label="About a trade you're in?" tooltip="Then it's saved on that trade, as a note while it runs">
                <Select allowClear placeholder="No, about my day" options={openTrades.map((t) => ({ value: t.id, label: `${t.symbol} (${t.side})` }))} />
              </Form.Item>
            </Col>
          )}
        </Row>
        <FeelingFields extraEmotions={options.emotions} placeholder="Tired after a loss? Itching to trade? Calm and patient?" />
        <Space style={{ color: MUTED, fontSize: 12 }}>Check-ins show on the day's page and count in the emotion statistics.</Space>
      </Form>
    </Modal>
  );
}
