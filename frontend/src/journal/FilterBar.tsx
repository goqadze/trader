// The journal's filters, shared by the trade list and the statistics (and the CSV export): search, dates, status,
// result, and more behind a button (setup, tag, feeling, mistake, account, source, side).

import { FilterOutlined } from "@ant-design/icons";
import { Badge, Button, DatePicker, Input, Select, Space } from "antd";
import dayjs from "dayjs";
import { useEffect, useState } from "react";
import { EMOTIONS, MISTAKES, emotionLabel, mistakeLabel, todayNY } from "./catalog";
import { useJournalOptions } from "./parts";
import type { JournalFilters } from "./types";

const DAY = "YYYY-MM-DD";
const MORE: (keyof JournalFilters)[] = ["setup", "tag", "emotion", "mistake", "account", "source", "side"];

/** Date ranges one click away. */
export function rangePresets() {
  const today = dayjs(todayNY());
  return [
    { label: "This week", value: [today.subtract((today.day() + 6) % 7, "day"), today] as [dayjs.Dayjs, dayjs.Dayjs] },
    { label: "This month", value: [today.startOf("month"), today] as [dayjs.Dayjs, dayjs.Dayjs] },
    { label: "Last 30 days", value: [today.subtract(29, "day"), today] as [dayjs.Dayjs, dayjs.Dayjs] },
    { label: "Last 90 days", value: [today.subtract(89, "day"), today] as [dayjs.Dayjs, dayjs.Dayjs] },
    { label: "This year", value: [today.startOf("year"), today] as [dayjs.Dayjs, dayjs.Dayjs] },
  ];
}

export default function FilterBar({ value, onChange, showStatus = true }: {
  value: JournalFilters;
  onChange: (f: JournalFilters) => void;
  showStatus?: boolean; // the statistics are about closed trades: no status filter there
}) {
  const { options } = useJournalOptions();
  const [q, setQ] = useState(value.q ?? "");
  const extra = MORE.filter((k) => value[k]).length;
  const [more, setMore] = useState(extra > 0);
  const set = (patch: Partial<JournalFilters>) => onChange({ ...value, ...patch });

  // Search as you type, a moment after the last key
  useEffect(() => setQ(value.q ?? ""), [value.q]);
  useEffect(() => {
    if ((value.q ?? "") === q) return;
    const t = setTimeout(() => set({ q: q || undefined }), 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const own = (values: string[], known: string[]) => values.filter((v) => !known.includes(v));
  const select = (key: keyof JournalFilters, placeholder: string, opts: { value: string; label: string }[], width = 150) => (
    <Select
      allowClear
      showSearch
      optionFilterProp="label"
      placeholder={placeholder}
      value={value[key] || undefined}
      onChange={(v) => set({ [key]: v || undefined })}
      options={opts}
      style={{ width }}
      popupMatchSelectWidth={false}
    />
  );
  const any = Object.values(value).some((v) => v);
  return (
    <Space direction="vertical" size={8} style={{ width: "100%", marginBottom: 12 }}>
      <Space wrap size={8}>
        <Input.Search allowClear value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search symbol, setup, notes…" style={{ width: 230 }} />
        <DatePicker.RangePicker
          allowEmpty={[true, true]}
          value={[value.start ? dayjs(value.start) : null, value.end ? dayjs(value.end) : null]}
          onChange={(r) => set({ start: r?.[0]?.format(DAY), end: r?.[1]?.format(DAY) })}
          presets={rangePresets()}
          placeholder={["From", "To"]}
          style={{ width: 240 }}
        />
        {showStatus && select("status", "Any status", [
          { value: "planned", label: "Planned" }, { value: "open", label: "Open" }, { value: "closed", label: "Closed" }, { value: "skipped", label: "Skipped" },
        ], 130)}
        {select("result", "Win or loss", [{ value: "win", label: "Wins" }, { value: "loss", label: "Losses" }, { value: "breakeven", label: "Breakeven" }], 130)}
        <Badge count={extra} size="small" color="#4f8cff">
          <Button icon={<FilterOutlined />} onClick={() => setMore(!more)} type={more ? "primary" : "default"} ghost={more}>More filters</Button>
        </Badge>
        {any && <Button type="link" onClick={() => { setQ(""); onChange({}); }}>Clear</Button>}
      </Space>
      {more && (
        <Space wrap size={8}>
          {select("setup", "Setup", options.setups.map((s) => ({ value: s, label: s })))}
          {select("tag", "Tag", options.tags.map((s) => ({ value: s, label: s })))}
          {select("emotion", "Feeling", [...EMOTIONS.map((e) => e.key), ...own(options.emotions, EMOTIONS.map((e) => e.key))].map((k) => ({ value: k, label: emotionLabel(k) })), 160)}
          {select("mistake", "Mistake", [...MISTAKES.map((m) => m.key), ...own(options.mistakes, MISTAKES.map((m) => m.key))].map((k) => ({ value: k, label: mistakeLabel(k) })), 170)}
          {select("account", "Account", options.accounts.map((s) => ({ value: s, label: s })))}
          {select("source", "Mine or a bot's", [{ value: "manual", label: "Written by me" }, { value: "bot", label: "Imported from bots" }], 150)}
          {select("side", "Long or short", [{ value: "long", label: "Long" }, { value: "short", label: "Short" }], 130)}
        </Space>
      )}
    </Space>
  );
}
