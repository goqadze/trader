import { Button, Form, Select, Space } from "antd";
import { ROTATION_GROUPS, SYMBOL_GROUPS, isCrypto, symbolOptions, type SymbolGroup } from "../symbols";

/** The 11 S&P 500 sector ETFs: a rotation's default universe (no hindsight in picking them, unlike today's top companies). */
export const SECTORS = SYMBOL_GROUPS.find((g) => g.short === "Sector ETFs")!.symbols.map((s) => s.symbol);

/** A rotation's universe: pick or type tickers, or add a whole suggested group. Use inside a Form. `noCrypto` for a
 *  live rotation bot, which can't trade crypto yet (a rotation backtest can). `min`: the fewest symbols allowed (a dip
 *  buyer's watchlist may have one). */
export default function UniverseField({ name, label = "Universe", noCrypto = false, min = 2 }: { name: string; label?: string; noCrypto?: boolean; min?: number }) {
  const form = Form.useFormInstance();
  const groups: SymbolGroup[] = noCrypto ? ROTATION_GROUPS.filter((g) => g.short !== "Crypto") : ROTATION_GROUPS;
  const options = symbolOptions((s) => `${s.symbol} · ${s.name}`, groups);
  const add = (more: string[]) => form.setFieldValue(name, [...new Set([...(form.getFieldValue(name) ?? []), ...more])]);
  return (
    <Form.Item
      label={label}
      required
      extra={
        <Space size={0} wrap>
          {groups.map((g) => (
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
        rules={[
          { required: true, type: "array", min, max: 60, message: `Pick ${min} to 60 symbols` },
          {
            validator: (_, vals: string[] = []) => {
              const coins = noCrypto ? vals.filter(isCrypto) : [];
              return coins.length ? Promise.reject(new Error(`Bots can't trade crypto yet: ${coins.join(", ")}`)) : Promise.resolve();
            },
          },
        ]}
        normalize={(vals: string[]) => [...new Set(vals.map((s) => s.trim().toUpperCase()).filter(Boolean))]}
      >
        <Select mode="tags" options={options} optionLabelProp="value" tokenSeparators={[",", " "]} placeholder="Pick or type tickers" />
      </Form.Item>
    </Form.Item>
  );
}
