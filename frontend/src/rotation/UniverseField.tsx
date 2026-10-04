import { Button, Form, Select, Space } from "antd";
import { SYMBOL_GROUPS, symbolOptions } from "../symbols";

/** The 11 S&P 500 sector ETFs: a rotation's default universe (no hindsight in picking them, unlike today's top companies). */
export const SECTORS = SYMBOL_GROUPS.find((g) => g.short === "Sector ETFs")!.symbols.map((s) => s.symbol);
const OPTIONS = symbolOptions((s) => `${s.symbol} · ${s.name}`);

/** A rotation's universe: pick or type tickers, or add a whole suggested group. Use inside a Form. */
export default function UniverseField({ name, label = "Universe" }: { name: string; label?: string }) {
  const form = Form.useFormInstance();
  const add = (more: string[]) => form.setFieldValue(name, [...new Set([...(form.getFieldValue(name) ?? []), ...more])]);
  return (
    <Form.Item
      label={label}
      required
      extra={
        <Space size={0} wrap>
          {SYMBOL_GROUPS.map((g) => (
            <Button key={g.label} type="link" size="small" style={{ paddingInline: 4 }} onClick={() => add(g.symbols.map((s) => s.symbol))}>
              + {g.short}
            </Button>
          ))}
          <Button type="link" size="small" style={{ paddingInline: 4 }} onClick={() => form.setFieldValue(name, [])}>
            clear
          </Button>
        </Space>
      }
    >
      <Form.Item
        name={name}
        noStyle
        rules={[{ required: true, type: "array", min: 2, max: 60, message: "Pick 2 to 60 symbols" }]}
        normalize={(vals: string[]) => [...new Set(vals.map((s) => s.trim().toUpperCase()).filter(Boolean))]}
      >
        <Select mode="tags" options={OPTIONS} optionLabelProp="value" tokenSeparators={[",", " "]} placeholder="Pick or type tickers" />
      </Form.Item>
    </Form.Item>
  );
}
