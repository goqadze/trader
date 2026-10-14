// Form pieces the journal's forms share: picking emotions and how strong they are, 1-5 ratings, yes / no, mistakes.

import { PlusOutlined } from "@ant-design/icons";
import { Form, Input, Rate, Segmented, Select, Space, Tag } from "antd";
import { useEffect, useState, type ReactNode } from "react";
import { journalApi } from "./api";
import { EMOTIONS, MISTAKES, MUTED, RATING_WORDS, STAGES, TONE_LABEL, emotion, mistakeLabel, type Tone } from "./catalog";
import type { JournalOptions, JournalStage } from "./types";

const EMPTY_OPTIONS: JournalOptions = { symbols: [], accounts: [], setups: [], tags: [], mistakes: [], emotions: [] };
let cached: JournalOptions | null = null;

/** What the user wrote before (symbols, setups, tags ...), for suggestions. Loaded once per page load, refreshed by
 *  `refresh()` after a save. */
export function useJournalOptions() {
  const [options, setOptions] = useState<JournalOptions>(cached ?? EMPTY_OPTIONS);
  const refresh = () => journalApi.options().then((o) => { cached = o; setOptions(o); }, () => undefined);
  useEffect(() => {
    if (!cached) void refresh();
  }, []);
  return { options, refresh };
}

const asOptions = (values: string[], label: (v: string) => string = (v) => v) => values.map((v) => ({ value: v, label: label(v) }));

/** Choosing emotions: the usual ones by tone, plus your own words (Enter adds one). */
export function EmotionChips({ value = [], onChange, extra = [] }: { value?: string[]; onChange?: (v: string[]) => void; extra?: string[] }) {
  const [typing, setTyping] = useState("");
  const toggle = (key: string) => onChange?.(value.includes(key) ? value.filter((v) => v !== key) : [...value, key]);
  const add = () => {
    const word = typing.trim().toLowerCase().replace(/\s+/g, " ");
    if (word && !value.includes(word)) onChange?.([...value, word]);
    setTyping("");
  };
  const own = [...new Set([...extra, ...value])].filter((k) => !EMOTIONS.some((e) => e.key === k));
  return (
    <Space direction="vertical" size={6} style={{ width: "100%" }}>
      {(["helps", "caution", "hurts"] as Tone[]).map((tone) => (
        <div key={tone}>
          <div style={{ fontSize: 11, color: MUTED, marginBottom: 2 }}>{TONE_LABEL[tone]}</div>
          {EMOTIONS.filter((e) => e.tone === tone).map((e) => (
            <Tag.CheckableTag
              key={e.key}
              checked={value.includes(e.key)}
              onChange={() => toggle(e.key)}
              style={{ marginBottom: 4, border: `1px solid ${value.includes(e.key) ? "transparent" : "#2a3550"}`, fontSize: 13, padding: "1px 8px" }}
            >
              <span style={{ marginRight: 4 }}>{e.emoji}</span>{e.label}
            </Tag.CheckableTag>
          ))}
        </div>
      ))}
      <div>
        {own.map((k) => (
          <Tag.CheckableTag key={k} checked={value.includes(k)} onChange={() => toggle(k)} style={{ marginBottom: 4, border: "1px solid #2a3550" }}>
            {emotion(k).label}
          </Tag.CheckableTag>
        ))}
        <Input
          size="small"
          value={typing}
          onChange={(e) => setTyping(e.target.value)}
          onPressEnter={(e) => { e.preventDefault(); add(); }}
          onBlur={add}
          maxLength={32}
          placeholder="Another feeling…"
          prefix={<PlusOutlined style={{ color: MUTED }} />}
          style={{ width: 170 }}
        />
      </div>
    </Space>
  );
}

/** A 1-5 rating with words on hover; click the chosen one again to clear it. */
export function RatingPicker({ value, onChange, words, character }: {
  value?: number | null; onChange?: (v: number | null) => void; words: keyof typeof RATING_WORDS; character?: ReactNode;
}) {
  return (
    <Space size={8}>
      <Rate value={value ?? 0} onChange={(v) => onChange?.(v || null)} tooltips={RATING_WORDS[words]} character={character} />
      <span style={{ color: MUTED, fontSize: 12 }}>{value ? RATING_WORDS[words][value - 1] : "not rated"}</span>
    </Space>
  );
}

const DOT = <span style={{ fontSize: 16 }}>●</span>;

/** The three fields of a note: how you feel, how strongly, and what you think. `name` is the form's note field. */
export function FeelingFields({ name = "note", extraEmotions = [], emotionsLabel = "How do you feel?", textLabel = "What's on your mind?", placeholder }: {
  name?: string; extraEmotions?: string[]; emotionsLabel?: string; textLabel?: string; placeholder?: string;
}) {
  return (
    <>
      <Form.Item name={[name, "emotions"]} label={emotionsLabel} style={{ marginBottom: 8 }}>
        <EmotionChips extra={extraEmotions} />
      </Form.Item>
      <Form.Item name={[name, "intensity"]} label="How strongly?" style={{ marginBottom: 8 }}>
        <RatingPicker words="intensity" character={DOT} />
      </Form.Item>
      <Form.Item name={[name, "text"]} label={textLabel} style={{ marginBottom: 8 }}>
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} maxLength={5000} placeholder={placeholder} />
      </Form.Item>
    </>
  );
}

/** A note's fields from the form, or nothing when nothing was written (no emotion, no words). */
export function noteFrom(v: { emotions?: string[]; intensity?: number | null; text?: string } | undefined) {
  if (!v || (!(v.emotions?.length) && !v.text?.trim())) return null;
  return { emotions: v.emotions ?? [], intensity: v.intensity ?? null, text: v.text?.trim() ?? "" };
}

/** Yes / No / not said, for a nullable boolean. */
export function YesNo({ value, onChange, yes = "Yes", no = "No" }: { value?: boolean | null; onChange?: (v: boolean | null) => void; yes?: string; no?: string }) {
  return (
    <Segmented
      value={value == null ? "unset" : value ? "yes" : "no"}
      onChange={(v) => onChange?.(v === "unset" ? null : v === "yes")}
      options={[{ value: "yes", label: yes }, { value: "no", label: no }, { value: "unset", label: "—" }]}
    />
  );
}

/** Mistakes: the classic ones, the user's own from before, or a new one typed in. Typing filters the list (Enter picks
 *  the first match); a new mistake is the last option, "Add …", so typing "moved" + Enter picks "Moved my stop". */
export function MistakeSelect({ value = [], onChange, used = [] }: { value?: string[]; onChange?: (v: string[]) => void; used?: string[] }) {
  const [search, setSearch] = useState("");
  const typed = search.trim().replace(/\s+/g, " ");
  const options = [...MISTAKES.map((m) => ({ value: m.key, label: m.label })),
    ...asOptions([...new Set([...used, ...value])].filter((m) => !MISTAKES.some((x) => x.key === m)))];
  const known = options.some((o) => o.value.toLowerCase() === typed.toLowerCase() || o.label.toLowerCase() === typed.toLowerCase());
  return (
    <Select
      mode="multiple"
      value={value}
      onChange={(v) => { setSearch(""); onChange?.(v); }}
      searchValue={search}
      onSearch={setSearch}
      placeholder="None — or pick / type what went wrong"
      options={typed && !known ? [...options, { value: typed.slice(0, 60), label: `Add "${typed.slice(0, 60)}"` }] : options}
      tagRender={({ value: v, closable, onClose }) => (
        <Tag color="volcano" closable={closable} onClose={onClose} style={{ marginInlineEnd: 4 }}>{mistakeLabel(String(v))}</Tag>
      )}
      optionFilterProp="label"
      maxCount={20}
      style={{ width: "100%" }}
    />
  );
}

/** Choosing a note's stage: each one's emoji above its name, so all five fit a phone's width (it scrolls if not). */
export function StagePicker({ value, onChange, stages }: { value?: JournalStage; onChange?: (v: JournalStage) => void; stages: JournalStage[] }) {
  return (
    <div style={{ overflowX: "auto", maxWidth: "100%" }}>
      <Segmented
        value={value}
        onChange={(v) => onChange?.(v as JournalStage)}
        options={stages.map((st) => ({
          value: st,
          label: (
            <div style={{ lineHeight: 1.25, padding: "3px 0" }}>
              <div>{STAGES[st].emoji}</div>
              <div style={{ fontSize: 12 }}>{STAGES[st].label}</div>
            </div>
          ),
        }))}
      />
    </div>
  );
}

/** Free tags: the user's own from before, or a new one. */
export function TagSelect({ value, onChange, used = [] }: { value?: string[]; onChange?: (v: string[]) => void; used?: string[] }) {
  return <Select mode="tags" value={value} onChange={onChange} options={asOptions(used)} placeholder="e.g. swing, earnings week" maxCount={20} style={{ width: "100%" }} />;
}

export function Hint({ children }: { children: ReactNode }) {
  return <div style={{ fontSize: 12, color: MUTED, marginTop: -4, marginBottom: 8 }}>{children}</div>;
}
