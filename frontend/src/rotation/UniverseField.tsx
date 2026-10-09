import { AppstoreAddOutlined, CloseOutlined } from "@ant-design/icons";
import { Button, Form, Space, Tag, theme } from "antd";
import { useState } from "react";
import SymbolPicker from "../components/SymbolPicker";
import { ROTATION_GROUPS, SYMBOL_GROUPS, isCrypto, type SymbolGroup } from "../symbols";

const MUTED = "#8b98b5";
const MAX = 60;

/** The 11 S&P 500 sector ETFs: a rotation's default universe (no hindsight in picking them, unlike today's top companies). */
export const SECTORS = SYMBOL_GROUPS.find((g) => g.short === "Sector ETFs")!.symbols.map((s) => s.symbol);

/** The chosen symbols as tags, and the buttons that open the picker or clear them. Form.Item passes value/onChange. */
function UniverseInput({ value = [], onChange, groups, label }: { value?: string[]; onChange?: (v: string[]) => void; groups: SymbolGroup[]; label: string }) {
  const { token } = theme.useToken();
  const { status } = Form.Item.useStatus();
  const [open, setOpen] = useState(false);
  const names = new Map(groups.flatMap((g) => g.symbols.map((s) => [s.symbol, s.name] as const)));
  const set = (v: string[]) => onChange?.(v);
  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen(true);
          }
        }}
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 4,
          minHeight: 40,
          maxHeight: 156,
          overflowY: "auto",
          padding: 6,
          cursor: "pointer",
          borderRadius: token.borderRadius,
          border: `1px solid ${status === "error" ? token.colorError : token.colorBorder}`,
          background: token.colorBgContainer,
        }}
      >
        {value.length ? (
          value.map((s) => (
            <Tag
              key={s}
              closable
              title={names.get(s)}
              onClose={(e) => {
                e.preventDefault();
                e.stopPropagation(); // remove it without opening the picker
                set(value.filter((x) => x !== s));
              }}
              style={{ marginInlineEnd: 0, fontSize: 13 }}
            >
              {s}
            </Tag>
          ))
        ) : (
          <span style={{ color: MUTED, alignSelf: "center", paddingInline: 4 }}>No symbols yet: click to choose</span>
        )}
      </div>
      <Space size={8} style={{ marginTop: 8 }} wrap>
        <Button size="small" icon={<AppstoreAddOutlined />} onClick={() => setOpen(true)}>
          Choose symbols
        </Button>
        <Button size="small" icon={<CloseOutlined />} disabled={!value.length} onClick={() => set([])}>
          Clear
        </Button>
        <span style={{ color: MUTED, fontSize: 12 }}>{value.length} selected</span>
      </Space>
      <SymbolPicker
        open={open}
        value={value}
        groups={groups}
        max={MAX}
        title={`Choose the ${label.toLowerCase()}`}
        onCancel={() => setOpen(false)}
        onApply={(v) => {
          set(v);
          setOpen(false);
        }}
      />
    </>
  );
}

/** A rotation's universe: pick symbols from the suggested groups (or type others) in a picker. Use inside a Form.
 *  `noCrypto` for a live bot, which can't trade crypto yet (a rotation backtest can). `min`: the fewest symbols
 *  allowed (a dip buyer's watchlist may have one). */
export default function UniverseField({ name, label = "Universe", noCrypto = false, min = 2 }: { name: string; label?: string; noCrypto?: boolean; min?: number }) {
  const groups: SymbolGroup[] = noCrypto ? ROTATION_GROUPS.filter((g) => g.short !== "Crypto") : ROTATION_GROUPS;
  return (
    <Form.Item
      name={name}
      label={label}
      required
      rules={[
        { required: true, type: "array", min, max: MAX, message: `Pick ${min} to ${MAX} symbols` },
        {
          validator: (_, vals: string[] = []) => {
            const coins = noCrypto ? vals.filter(isCrypto) : [];
            return coins.length ? Promise.reject(new Error(`Bots can't trade crypto yet: ${coins.join(", ")}`)) : Promise.resolve();
          },
        },
      ]}
      normalize={(vals: string[]) => [...new Set(vals.map((s) => s.trim().toUpperCase()).filter(Boolean))]}
    >
      <UniverseInput groups={groups} label={label} />
    </Form.Item>
  );
}
