// One journal trade: its plan, what happened, the review, the notes step by step (how you felt at each), screenshots.
// The button on top is the next step: enter a planned trade, close an open one, review a closed one.

import { ArrowLeftOutlined, DeleteOutlined, EditOutlined, MoreOutlined, PlusOutlined, RobotOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, Descriptions, Dropdown, Empty, Row, Space, Spin, Statistic, Tag, Timeline, Typography } from "antd";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { localTime, pct, pnlColor, usd } from "../trading/format";
import { journalApi } from "./api";
import {
  EmotionTag, MUTED, RATING_WORDS, STAGES, StatusTag, dayLabel, holdText, mistakeLabel, price, qtyText, rText,
} from "./catalog";
import Screenshots from "./Screenshots";
import { StepModal, type Step } from "./StepModal";
import TradeForm from "./TradeForm";
import type { JournalNote, JournalTradeDetail } from "./types";

const NEXT: Record<JournalTradeDetail["status"], { step: Step; label: string }[]> = {
  planned: [{ step: "enter", label: "Enter the trade" }, { step: "skip", label: "Skip it" }],
  open: [{ step: "close", label: "Close the trade" }],
  closed: [{ step: "review", label: "Review it" }],
  skipped: [],
};

const WRAP = { marginInlineEnd: 0, whiteSpace: "normal", wordBreak: "break-word" } as const; // a long tag wraps on a phone
const text = (s: string) => (s ? <span style={{ whiteSpace: "pre-wrap" }}>{s}</span> : <span style={{ color: MUTED }}>—</span>);

function Intensity({ value }: { value: number | null }) {
  if (!value) return null;
  return (
    <span title={`Intensity: ${RATING_WORDS.intensity[value - 1]}`} style={{ color: "#f0a43a", letterSpacing: 1, fontSize: 11 }}>
      {"●".repeat(value)}<span style={{ color: "#2a3550" }}>{"●".repeat(5 - value)}</span>
    </span>
  );
}

/** One note as the timeline shows it: the stage, when, the emotions, how strong, the words. */
export function NoteBody({ note, actions }: { note: JournalNote; actions?: ReactNode }) {
  return (
    <div>
      <Space size={8} wrap style={{ marginBottom: 4 }}>
        <b>{STAGES[note.stage].emoji} {STAGES[note.stage].label}</b>
        <span style={{ color: MUTED, fontSize: 12 }}>{localTime(note.at)}</span>
        <Intensity value={note.intensity} />
        {actions}
      </Space>
      {note.emotions.length > 0 && <div style={{ marginBottom: 4 }}>{note.emotions.map((e) => <EmotionTag key={e} name={e} />)}</div>}
      {note.text && <div style={{ whiteSpace: "pre-wrap" }}>{note.text}</div>}
    </div>
  );
}

export default function TradePage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { message, modal } = AntApp.useApp();
  const [trade, setTrade] = useState<JournalTradeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState<Step | null>(null);
  const [editingNote, setEditingNote] = useState<JournalNote | null>(null);
  const [editing, setEditing] = useState(false);

  const load = useCallback(() => {
    journalApi.trade(Number(id)).then((t) => { setTrade(t); setError(null); }, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [id]);
  useEffect(load, [load]);

  if (error) return <Alert type="error" showIcon message={error} action={<Link to="/journal">Back to the journal</Link>} />;
  if (!trade) return <Spin style={{ display: "block", margin: 48 }} />;

  const saved = (t: JournalTradeDetail) => {
    setTrade(t);
    setStep(null);
    setEditingNote(null);
    setEditing(false);
  };
  const removeTrade = () =>
    modal.confirm({
      title: `Delete ${trade.symbol} from your journal?`,
      content: `Its ${trade.notes} note(s) and ${trade.images} screenshot(s) go with it. This can't be undone.`,
      okText: "Delete",
      okButtonProps: { danger: true },
      onOk: async () => {
        await journalApi.deleteTrade(trade.id);
        message.success("Deleted");
        navigate("/journal");
      },
    });
  const removeNote = (n: JournalNote) =>
    modal.confirm({
      title: "Delete this note?",
      okText: "Delete",
      okButtonProps: { danger: true },
      onOk: async () => {
        await journalApi.deleteNote(n.id);
        load();
      },
    });

  const closed = trade.status === "closed";
  const reviewed = trade.followed_plan != null || trade.grade != null || trade.review || trade.lessons || trade.mistakes.length > 0;
  const next = NEXT[trade.status];
  return (
    <>
      <Link to="/journal" style={{ color: MUTED }}><ArrowLeftOutlined /> Journal</Link>
      <Space align="center" wrap style={{ width: "100%", justifyContent: "space-between", margin: "8px 0 12px" }}>
        <Space align="center" wrap size={8}>
          <Typography.Title level={3} style={{ margin: 0 }}>{trade.symbol}</Typography.Title>
          <Tag color={trade.side === "long" ? "green" : "volcano"}>{trade.side}</Tag>
          <StatusTag status={trade.status} />
          {trade.source === "bot" && <Tag icon={<RobotOutlined />} color="purple">bot</Tag>}
          <span style={{ color: MUTED }}>{[trade.setup, trade.account, dayLabel(trade.day)].filter(Boolean).join(" · ")}</span>
        </Space>
        <Space wrap>
          {next.map((n, i) => (
            <Button key={n.step} type={i === 0 && !(n.step === "review" && reviewed) ? "primary" : "default"} onClick={() => setStep(n.step)}>
              {n.step === "review" && reviewed ? "Edit the review" : n.label}
            </Button>
          ))}
          <Button icon={<PlusOutlined />} onClick={() => setStep("note")}>Note</Button>
          <Dropdown
            trigger={["click"]}
            menu={{ items: [
              { key: "edit", icon: <EditOutlined />, label: "Edit everything", onClick: () => setEditing(true) },
              { key: "delete", icon: <DeleteOutlined />, label: "Delete", danger: true, onClick: removeTrade },
            ] }}
          >
            <Button icon={<MoreOutlined />} aria-label="More" />
          </Dropdown>
        </Space>
      </Space>

      {trade.source === "bot" && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
               message={<>Imported from {trade.bot_id ? <Link to={`/trading/${trade.bot_id}`}>{trade.bot_name ?? `bot #${trade.bot_id}`}</Link> : "a bot"}: the bot traded it. Journal how you felt watching it, and review the bot's call.</>} />
      )}

      <Row gutter={[12, 12]} style={{ marginBottom: 12 }}>
        <Col xs={12} md={6}><Card size="small"><Statistic title="P&L" value={trade.pnl != null ? usd(trade.pnl) : "—"} valueStyle={{ color: pnlColor(trade.pnl) }} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="R multiple" value={rText(trade.r_multiple)} valueStyle={{ color: pnlColor(trade.r_multiple) }} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Return" value={pct(trade.return_pct)} valueStyle={{ color: pnlColor(trade.return_pct) }} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title={closed ? "Held" : "Risk (1R)"} value={closed ? holdText(trade.hold_minutes) : trade.risk != null ? usd(trade.risk) : "—"} /></Card></Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={14}>
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Card size="small" title="Step by step: what you felt and thought"
                  extra={<Button size="small" type="link" icon={<PlusOutlined />} onClick={() => setStep("note")}>Add a note</Button>}>
              {trade.note_list.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No notes yet. Write how you feel at each step: it's what you'll learn the most from." />
              ) : (
                <Timeline
                  style={{ marginTop: 8, marginBottom: -16 }}
                  items={trade.note_list.map((n) => ({
                    color: STAGES[n.stage].dot,
                    children: (
                      <NoteBody
                        note={n}
                        actions={
                          <Space size={0}>
                            <Button type="link" size="small" onClick={() => { setEditingNote(n); setStep("note"); }}>Edit</Button>
                            <Button type="link" size="small" danger onClick={() => removeNote(n)}>Delete</Button>
                          </Space>
                        }
                      />
                    ),
                  }))}
                />
              )}
            </Card>
            <Screenshots tradeId={trade.id} images={trade.image_list} onChange={load} />
          </Space>
        </Col>
        <Col xs={24} lg={10}>
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Card size="small" title="The plan">
              <Descriptions size="small" column={2} colon={false} labelStyle={{ color: MUTED }}>
                <Descriptions.Item label="Entry">{price(trade.entry_price)}</Descriptions.Item>
                <Descriptions.Item label="Size">{qtyText(trade.quantity)}</Descriptions.Item>
                <Descriptions.Item label="Stop">{price(trade.stop_price)}</Descriptions.Item>
                <Descriptions.Item label="Target">{price(trade.target_price)}</Descriptions.Item>
                <Descriptions.Item label="Risk">{trade.risk != null ? usd(trade.risk) : "—"}</Descriptions.Item>
                <Descriptions.Item label="Reward:risk">{trade.reward_risk != null ? `${trade.reward_risk} : 1` : "—"}</Descriptions.Item>
                <Descriptions.Item label="Confidence" span={2}>{trade.confidence ? `${trade.confidence}/5 · ${RATING_WORDS.confidence[trade.confidence - 1]}` : "—"}</Descriptions.Item>
                {trade.tags.length > 0 && (
                  <Descriptions.Item label="Tags" span={2}>
                    <Space size={[4, 4]} wrap>{trade.tags.map((t) => <Tag key={t} style={WRAP}>{t}</Tag>)}</Space>
                  </Descriptions.Item>
                )}
              </Descriptions>
              <div style={{ color: MUTED, fontSize: 12, marginTop: 4 }}>Why this trade</div>
              {text(trade.thesis)}
            </Card>
            {trade.status !== "planned" && trade.status !== "skipped" && (
              <Card size="small" title="What happened">
                <Descriptions size="small" column={1} colon={false} labelStyle={{ color: MUTED, width: 90 }}>
                  <Descriptions.Item label="Entered">{trade.opened_at ? `${localTime(trade.opened_at)} at ${price(trade.entry_price)}` : "—"}</Descriptions.Item>
                  <Descriptions.Item label="Exited">{trade.closed_at ? `${localTime(trade.closed_at)} at ${price(trade.exit_price)}` : "still open"}</Descriptions.Item>
                  <Descriptions.Item label="Fees">{usd(trade.fees)}</Descriptions.Item>
                  {closed && <Descriptions.Item label="Why out">{text(trade.exit_reason)}</Descriptions.Item>}
                </Descriptions>
              </Card>
            )}
            {closed && (
              <Card size="small" title="Review" extra={<Button size="small" type="link" onClick={() => setStep("review")}>{reviewed ? "Edit" : "Review it"}</Button>}>
                {!reviewed ? (
                  <span style={{ color: MUTED }}>Not reviewed yet. Did you follow your plan? What would you do differently?</span>
                ) : (
                  <Descriptions size="small" column={1} colon={false} labelStyle={{ color: MUTED, width: 110 }}>
                    <Descriptions.Item label="Followed plan">
                      {trade.followed_plan == null ? "—" : trade.followed_plan ? <Tag color="green">Yes</Tag> : <Tag color="red">No</Tag>}
                    </Descriptions.Item>
                    <Descriptions.Item label="Grade">{trade.grade ? RATING_WORDS.grade[trade.grade - 1] : "—"}</Descriptions.Item>
                    <Descriptions.Item label="Mistakes">
                      {trade.mistakes.length ? (
                        <Space size={[4, 4]} wrap>
                          {trade.mistakes.map((m) => <Tag key={m} color="volcano" style={WRAP}>{mistakeLabel(m)}</Tag>)}
                        </Space>
                      ) : "none"}
                    </Descriptions.Item>
                    <Descriptions.Item label="Well / better">{text(trade.review)}</Descriptions.Item>
                    <Descriptions.Item label="Lesson">{text(trade.lessons)}</Descriptions.Item>
                  </Descriptions>
                )}
              </Card>
            )}
          </Space>
        </Col>
      </Row>

      <StepModal step={step} trade={trade} note={editingNote} onClose={() => { setStep(null); setEditingNote(null); }} onSaved={saved} />
      <TradeForm open={editing} trade={trade} onClose={() => setEditing(false)} onSaved={saved} />
    </>
  );
}
