import { CheckOutlined, CloseOutlined, PlusOutlined, SearchOutlined } from "@ant-design/icons";
import { Button, Empty, Grid, Input, Modal, Space, Tag, Typography, theme } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import type { SymbolGroup, SymbolInfo } from "../symbols";

const MUTED = "#8b98b5";

interface Props {
  open: boolean;
  value: string[];
  groups: SymbolGroup[];
  max?: number;
  title?: string;
  onCancel: () => void;
  onApply: (symbols: string[]) => void;
}

const clean = (s: string) => s.trim().toUpperCase();
const matches = (s: SymbolInfo, q: string) => !q || s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q);

/** Pick many symbols at once: the suggested groups as badges to switch on and off, a filter, a box for any other
 *  ticker, and clear buttons for everything or one group. Works on a copy: nothing changes until "Use". */
export default function SymbolPicker({ open, value, groups, max = 60, title = "Choose symbols", onCancel, onApply }: Props) {
  const { token } = theme.useToken();
  const screens = Grid.useBreakpoint();
  const [draft, setDraft] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typed, setTyped] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef<Record<string, HTMLDivElement | null>>({});

  // Load the field's value only when the modal opens (see BotForm)
  const valueRef = useRef(value);
  valueRef.current = value;
  useEffect(() => {
    if (open) {
      setDraft(valueRef.current);
      setQuery("");
      setTyped("");
    }
  }, [open]);

  const picked = useMemo(() => new Set(draft), [draft]);
  const q = query.trim().toLowerCase();
  const shown = groups.map((g) => ({ group: g, symbols: g.symbols.filter((s) => matches(s, q)) })).filter((x) => x.symbols.length);
  const names = useMemo(() => new Map(groups.flatMap((g) => g.symbols.map((s) => [s.symbol, s.name] as const))), [groups]);

  const toggle = (sym: string) => setDraft((d) => (d.includes(sym) ? d.filter((s) => s !== sym) : [...d, sym]));
  const addAll = (syms: string[]) => setDraft((d) => [...d, ...syms.filter((s) => !d.includes(s))]);
  const removeAll = (syms: string[]) => setDraft((d) => d.filter((s) => !syms.includes(s)));
  const addTyped = () => {
    const syms = typed.split(/[\s,]+/).map(clean).filter(Boolean);
    if (syms.length) addAll(syms);
    setTyped("");
  };
  const jump = (label: string) => {
    const el = sectionRefs.current[label];
    if (el) listRef.current?.scrollTo({ top: el.offsetTop, behavior: "smooth" });
  };

  const tooMany = draft.length > max;
  const badge = (s: SymbolInfo) => {
    const on = picked.has(s.symbol);
    return (
      <button
        key={s.symbol}
        type="button"
        title={`${s.symbol} · ${s.name}`}
        aria-pressed={on}
        onClick={() => toggle(s.symbol)}
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-start",
          gap: 2,
          minWidth: 0,
          padding: "6px 10px",
          textAlign: "left",
          cursor: "pointer",
          borderRadius: token.borderRadius,
          border: `1px solid ${on ? token.colorPrimary : token.colorBorderSecondary}`,
          background: on ? token.colorPrimaryBg : token.colorFillQuaternary,
          color: token.colorText,
          font: "inherit",
          transition: "border-color .15s, background .15s",
        }}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
          {on && <CheckOutlined style={{ color: token.colorPrimary, fontSize: 12 }} />}
          {s.symbol}
        </span>
        <span style={{ fontSize: 12, lineHeight: "16px", color: on ? token.colorTextSecondary : MUTED, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
          {s.name}
        </span>
      </button>
    );
  };

  return (
    <Modal
      title={title}
      open={open}
      onCancel={onCancel}
      width={Math.min(1000, window.innerWidth - 32)}
      style={{ top: 32 }}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ color: tooMany ? token.colorError : MUTED, marginRight: "auto" }}>
            {tooMany ? `${draft.length} selected: at most ${max}` : `${draft.length} selected`}
          </span>
          <Button onClick={onCancel}>Cancel</Button>
          <Button type="primary" disabled={tooMany} onClick={() => onApply(draft)}>
            Use {draft.length === 1 ? "this symbol" : `these ${draft.length} symbols`}
          </Button>
        </div>
      }
    >
      <Input
        allowClear
        prefix={<SearchOutlined style={{ color: MUTED }} />}
        placeholder="Filter by ticker or name (e.g. tower, solar, XL)"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{ marginBottom: 12 }}
      />

      <div style={{ padding: 10, marginBottom: 12, borderRadius: token.borderRadiusLG, border: `1px solid ${token.colorBorderSecondary}`, background: token.colorFillQuaternary }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
          <Typography.Text strong>Selected ({draft.length})</Typography.Text>
          <Space.Compact size="small" style={{ marginLeft: "auto", minWidth: 220, flex: "0 1 320px" }}>
            <Input
              size="small"
              placeholder="Any other ticker, e.g. BEP or COST"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onPressEnter={addTyped}
            />
            <Button size="small" icon={<PlusOutlined />} disabled={!typed.trim()} onClick={addTyped}>Add</Button>
          </Space.Compact>
          <Button size="small" danger icon={<CloseOutlined />} disabled={!draft.length} onClick={() => setDraft([])}>
            Clear all
          </Button>
        </div>
        {draft.length ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, maxHeight: 92, overflowY: "auto" }}>
            {draft.map((s) => (
              <Tag key={s} closable onClose={() => toggle(s)} title={names.get(s) ?? "typed by you"} color={names.has(s) ? "blue" : "gold"} style={{ marginInlineEnd: 0 }}>
                {s}
              </Tag>
            ))}
          </div>
        ) : (
          <span style={{ color: MUTED, fontSize: 12 }}>Nothing yet: click symbols below, or add a ticker that isn't listed.</span>
        )}
      </div>

      <div style={{ display: "flex", gap: 16 }}>
        {screens.md && (
          <nav style={{ flex: "0 0 190px", display: "flex", flexDirection: "column", gap: 2 }}>
            {groups.map((g) => {
              const n = g.symbols.filter((s) => picked.has(s.symbol)).length;
              const hidden = !shown.some((x) => x.group === g);
              return (
                <Button key={g.label} type="text" size="small" disabled={hidden} onClick={() => jump(g.label)} style={{ justifyContent: "space-between", display: "flex", textAlign: "left" }}>
                  <span>{g.short}</span>
                  <span style={{ color: n ? token.colorPrimary : MUTED, fontSize: 12 }}>{n ? `${n}/${g.symbols.length}` : g.symbols.length}</span>
                </Button>
              );
            })}
          </nav>
        )}
        <div ref={listRef} style={{ position: "relative", flex: 1, minWidth: 0, height: "52vh", overflowY: "auto", paddingRight: 4 }}>
          {shown.length === 0 && <Empty description={`Nothing listed matches "${query}". Add it as a ticker above.`} />}
          {shown.map(({ group: g, symbols }) => {
            const syms = symbols.map((s) => s.symbol);
            const n = syms.filter((s) => picked.has(s)).length;
            return (
              <div key={g.label} ref={(el) => { sectionRefs.current[g.label] = el; }} style={{ marginBottom: 18 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
                  <Typography.Text strong>{g.label}</Typography.Text>
                  <span style={{ color: n ? token.colorPrimary : MUTED, fontSize: 12 }}>{n} of {syms.length}</span>
                  <Space size={0} style={{ marginLeft: "auto" }}>
                    <Button type="link" size="small" disabled={n === syms.length} onClick={() => addAll(syms)}>Select all</Button>
                    <Button type="link" size="small" disabled={n === 0} onClick={() => removeAll(syms)}>Clear</Button>
                  </Space>
                </div>
                {g.note && <div style={{ color: MUTED, fontSize: 12, marginBottom: 8 }}>{g.note}</div>}
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 6 }}>{symbols.map(badge)}</div>
              </div>
            );
          })}
        </div>
      </div>
    </Modal>
  );
}
