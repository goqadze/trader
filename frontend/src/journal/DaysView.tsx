// The journal by day: a month calendar (each day's P&L, notes, mood), and the chosen day's page: the plan before the
// open, the review after the close, the check-ins and notes written that day, and its trades. Days are New York dates.

import { LeftOutlined, PlusOutlined, RightOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Card, Col, Empty, Form, Input, List, Row, Space, Spin, Tag, Tooltip, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { pnlColor, usd } from "../trading/format";
import { journalApi } from "./api";
import { GREEN, MOOD_EMOJI, MUTED, RED, StatusTag, compactUsd, dayLabel, todayNY } from "./catalog";
import { NoteBody } from "./TradePage";
import { RatingPicker, YesNo } from "./parts";
import { CheckInModal } from "./StepModal";
import type { JournalCalendarDay, JournalDay, JournalDayFields, JournalNote } from "./types";

const DAY = "YYYY-MM-DD";
const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const EMPTY_DAY: JournalDayFields = { mood: null, energy: null, sleep: null, plan: "", review: "", lessons: "", grade: null, followed_rules: null };

/** The 6 weeks shown for a month, Monday first. */
function gridDays(month: Dayjs): Dayjs[] {
  const first = month.startOf("month");
  const start = first.subtract((first.day() + 6) % 7, "day");
  return Array.from({ length: 42 }, (_, i) => start.add(i, "day"));
}

function MonthGrid({ month, days, selected, onSelect }: {
  month: Dayjs; days: Map<string, JournalCalendarDay>; selected: string; onSelect: (d: string) => void;
}) {
  const today = todayNY();
  const cells = gridDays(month);
  const lastRow = cells.slice(35).every((d) => d.month() !== month.month()) ? 35 : 42; // drop an all-next-month row
  return (
    <div role="grid" aria-label={month.format("MMMM YYYY")} style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0, 1fr))", gap: 4 }}>
      {WEEK.map((w) => <div key={w} style={{ textAlign: "center", fontSize: 11, color: MUTED }}>{w}</div>)}
      {cells.slice(0, lastRow).map((d) => {
        const key = d.format(DAY);
        const info = days.get(key);
        const inMonth = d.month() === month.month();
        const pnl = info?.closed ? info.pnl : null;
        const tint = pnl == null || Math.abs(pnl) < 0.005 ? "transparent" : pnl > 0 ? "rgba(51,192,136,0.16)" : "rgba(239,91,107,0.16)";
        return (
          <button
            key={key}
            type="button"
            onClick={() => onSelect(key)}
            aria-pressed={key === selected}
            aria-label={`${dayLabel(key)}${pnl != null ? `, ${usd(pnl)}` : ""}${info?.notes ? `, ${info.notes} notes` : ""}`}
            style={{
              minHeight: 58, padding: "3px 4px", borderRadius: 6, cursor: "pointer", textAlign: "left", background: tint,
              border: `1px solid ${key === selected ? "#4f8cff" : "#2a3550"}`, color: inMonth ? "#e6ebf5" : "#4a5672",
              display: "flex", flexDirection: "column", justifyContent: "space-between", overflow: "hidden", minWidth: 0,
            }}
          >
            <span style={{ fontSize: 12, fontWeight: key === today ? 700 : 400, color: key === today ? "#4f8cff" : undefined }}>
              {d.date()}
              {info?.mood && <span style={{ marginLeft: 2 }}>{MOOD_EMOJI[info.mood - 1]}</span>}
            </span>
            {pnl != null && (
              <span style={{ fontSize: 11, fontWeight: 600, color: pnl > 0.005 ? GREEN : pnl < -0.005 ? RED : MUTED, whiteSpace: "nowrap" }}>
                {compactUsd(pnl)}
              </span>
            )}
            <span style={{ fontSize: 10, color: MUTED, whiteSpace: "nowrap" }}>
              {info?.page ? "📝" : ""}{info?.notes ? `💬${info.notes}` : ""}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** The chosen day's page. */
function DayPage({ day, onChanged }: { day: string; onChanged: () => void }) {
  const { message, modal } = AntApp.useApp();
  const [form] = Form.useForm<JournalDayFields>();
  const [page, setPage] = useState<JournalDay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [checkIn, setCheckIn] = useState<{ note: JournalNote | null } | null>(null);

  const load = useCallback(() => {
    journalApi.day(day).then(
      (p) => {
        setPage(p);
        setError(null);
        form.setFieldsValue({ ...EMPTY_DAY, ...Object.fromEntries(Object.keys(EMPTY_DAY).map((k) => [k, p[k as keyof JournalDayFields]])) });
        setDirty(false);
      },
      (e) => setError(e instanceof Error ? e.message : String(e)),
    );
  }, [day, form]);
  useEffect(() => {
    setPage(null);
    load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      const v = form.getFieldsValue(true) as JournalDayFields;
      setPage(await journalApi.saveDay(day, { ...EMPTY_DAY, ...v, plan: v.plan ?? "", review: v.review ?? "", lessons: v.lessons ?? "" }));
      setDirty(false);
      message.success(`Saved ${dayLabel(day)}`);
      onChanged();
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e), 8);
    } finally {
      setSaving(false);
    }
  };
  const removeNote = (n: JournalNote) =>
    modal.confirm({
      title: "Delete this note?",
      okText: "Delete",
      okButtonProps: { danger: true },
      onOk: async () => {
        await journalApi.deleteNote(n.id);
        load();
        onChanged();
      },
    });

  if (error) return <Alert type="error" showIcon message={error} />;
  if (!page) return <Spin style={{ display: "block", margin: 32 }} />;
  const isToday = day === todayNY();
  // A check-in for a past day is dated noon (New York) of that day; today's is now
  const checkInAt = isToday ? undefined : dayjs(new Date(`${day}T12:00:00-04:00`));
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space align="baseline" wrap style={{ width: "100%", justifyContent: "space-between" }}>
        <Typography.Title level={5} style={{ margin: 0 }}>{dayLabel(day)}{isToday && <Tag color="blue" style={{ marginLeft: 8 }}>today</Tag>}</Typography.Title>
        {page.trades.some((t) => t.status === "closed") && <span>Closed today: <b style={{ color: pnlColor(page.pnl) }}>{usd(page.pnl)}</b></span>}
      </Space>
      <Form<JournalDayFields> form={form} layout="vertical" requiredMark={false} onValuesChange={() => setDirty(true)}>
        <Row gutter={[12, 12]}>
          <Col xs={24} xl={12}>
            <Card size="small" title="☀️ Before the open" style={{ height: "100%" }}>
              <Form.Item name="mood" label="Mood" style={{ marginBottom: 4 }}><RatingPicker words="mood" /></Form.Item>
              <Form.Item name="energy" label="Energy" style={{ marginBottom: 4 }}><RatingPicker words="energy" /></Form.Item>
              <Form.Item name="sleep" label="Sleep" style={{ marginBottom: 4 }}><RatingPicker words="sleep" /></Form.Item>
              <Form.Item name="plan" label="The plan" tooltip="The market today, what you watch, your rules and goals for the day" style={{ marginBottom: 0 }}>
                <Input.TextArea autoSize={{ minRows: 3, maxRows: 12 }} maxLength={10000}
                                placeholder={"Market: …\nWatching: …\nRules today: max 2 trades, no trades in the first 15 minutes"} />
              </Form.Item>
            </Card>
          </Col>
          <Col xs={24} xl={12}>
            <Card size="small" title="🌙 After the close" style={{ height: "100%" }}>
              <Form.Item name="grade" label="How well did you trade?" tooltip="Discipline and execution, not the P&L" style={{ marginBottom: 4 }}>
                <RatingPicker words="grade" />
              </Form.Item>
              <Form.Item name="followed_rules" label="Kept to your rules?" style={{ marginBottom: 8 }}><YesNo /></Form.Item>
              <Form.Item name="review" label="How did it go?" style={{ marginBottom: 8 }}>
                <Input.TextArea autoSize={{ minRows: 2, maxRows: 12 }} maxLength={10000} placeholder="What went well, what didn't" />
              </Form.Item>
              <Form.Item name="lessons" label="Lesson" style={{ marginBottom: 0 }}>
                <Input.TextArea autoSize={{ minRows: 1, maxRows: 8 }} maxLength={10000} />
              </Form.Item>
            </Card>
          </Col>
        </Row>
        <Space style={{ marginTop: 12 }}>
          <Button type="primary" onClick={save} loading={saving} disabled={!dirty}>Save the day</Button>
          {dirty && <span style={{ color: MUTED, fontSize: 12 }}>Unsaved changes</span>}
        </Space>
      </Form>

      <Card size="small" title="💬 Check-ins and notes"
            extra={<Button size="small" icon={<PlusOutlined />} onClick={() => setCheckIn({ note: null })}>Check in</Button>}>
        {page.notes.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="Nothing yet. A quick check-in when your mood shifts (after a loss, before a big decision) is the habit that pays." />
        ) : (
          <List
            size="small"
            dataSource={page.notes}
            renderItem={(n) => (
              <List.Item style={{ paddingInline: 0 }}>
                <NoteBody
                  note={n}
                  actions={n.trade_id ? <Link to={`/journal/trades/${n.trade_id}`}>{n.symbol}</Link> : (
                    <Space size={0}>
                      <Button type="link" size="small" onClick={() => setCheckIn({ note: n })}>Edit</Button>
                      <Button type="link" size="small" danger onClick={() => removeNote(n)}>Delete</Button>
                    </Space>
                  )}
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      <Card size="small" title="Trades">
        {page.trades.length === 0 ? <span style={{ color: MUTED }}>No trades this day</span> : (
          <List
            size="small"
            dataSource={page.trades}
            renderItem={(t) => (
              <List.Item style={{ paddingInline: 0 }} extra={t.pnl != null ? <b style={{ color: pnlColor(t.pnl) }}>{usd(t.pnl)}</b> : null}>
                <Space size={8} wrap>
                  <Link to={`/journal/trades/${t.id}`}><b>{t.symbol}</b></Link>
                  <StatusTag status={t.status} />
                  <span style={{ color: MUTED, fontSize: 12 }}>{t.setup}</span>
                </Space>
              </List.Item>
            )}
          />
        )}
      </Card>

      <CheckInModal open={!!checkIn} note={checkIn?.note} at={checkInAt}
                    openTrades={page.trades.filter((t) => t.status === "open")}
                    onClose={() => setCheckIn(null)} onSaved={() => { setCheckIn(null); load(); onChanged(); }} />
    </Space>
  );
}

export default function DaysView({ reloadKey }: { reloadKey: number }) {
  const [month, setMonth] = useState(() => dayjs(todayNY()).startOf("month"));
  const [selected, setSelected] = useState(todayNY());
  const [days, setDays] = useState<Map<string, JournalCalendarDay>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const cells = gridDays(month);
    journalApi.calendar(cells[0].format(DAY), cells[41].format(DAY)).then(
      (list) => { setDays(new Map(list.map((d) => [d.day, d]))); setError(null); },
      (e) => setError(e instanceof Error ? e.message : String(e)),
    );
  }, [month, tick, reloadKey]);

  const summary = useMemo(() => {
    const inMonth = [...days.values()].filter((d) => dayjs(d.day).month() === month.month());
    const traded = inMonth.filter((d) => d.closed);
    return {
      pnl: traded.reduce((s, d) => s + d.pnl, 0),
      green: traded.filter((d) => d.pnl > 0.005).length,
      red: traded.filter((d) => d.pnl < -0.005).length,
      journaled: inMonth.filter((d) => d.page || d.notes).length,
      any: traded.length > 0,
    };
  }, [days, month]);

  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} lg={10}>
        <Card size="small">
          <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 8 }}>
            <Button type="text" icon={<LeftOutlined />} aria-label="Previous month" onClick={() => setMonth(month.subtract(1, "month"))} />
            <b>{month.format("MMMM YYYY")}</b>
            <Space size={0}>
              <Button type="link" size="small" onClick={() => { setMonth(dayjs(todayNY()).startOf("month")); setSelected(todayNY()); }}>Today</Button>
              <Button type="text" icon={<RightOutlined />} aria-label="Next month" onClick={() => setMonth(month.add(1, "month"))} />
            </Space>
          </Space>
          {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 8 }} />}
          <MonthGrid month={month} days={days} selected={selected}
                     onSelect={(d) => { setSelected(d); if (!dayjs(d).isSame(month, "month")) setMonth(dayjs(d).startOf("month")); }} />
          <Space size={12} wrap style={{ marginTop: 10, fontSize: 12, color: MUTED }}>
            {summary.any && <span>Month: <b style={{ color: pnlColor(summary.pnl) }}>{usd(summary.pnl)}</b></span>}
            {summary.any && <Tooltip title="Days whose closed trades made / lost money"><span><span style={{ color: GREEN }}>{summary.green} green</span> · <span style={{ color: RED }}>{summary.red} red</span></span></Tooltip>}
            <span>{summary.journaled} day(s) journaled</span>
            <span>📝 day page · 💬 notes</span>
          </Space>
        </Card>
      </Col>
      <Col xs={24} lg={14}>
        <DayPage key={selected} day={selected} onChanged={() => setTick((t) => t + 1)} />
      </Col>
    </Row>
  );
}
