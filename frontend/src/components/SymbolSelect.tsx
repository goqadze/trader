import { AutoComplete } from "antd";
import { SYMBOL_GROUPS } from "../symbols";

const MUTED = "#8b98b5";

const OPTIONS = SYMBOL_GROUPS.map((g) => ({
  label: g.label,
  options: g.symbols.map((s) => ({
    value: s.symbol,
    search: `${s.symbol} ${s.name}`.toLowerCase(),
    label: (
      <span>
        <b>{s.symbol}</b> <span style={{ color: MUTED }}>{s.name}</span>
      </span>
    ),
  })),
}));

interface Props {
  value?: string;
  onChange?: (value: string) => void;
}

// Symbol field: a dropdown of suggested tickers that still takes any typed ticker. Typing filters the
// suggestions by ticker or name; whatever is in the box when the form submits is the symbol.
// value/onChange come from the surrounding Form.Item.
export default function SymbolSelect({ value, onChange }: Props) {
  return (
    <AutoComplete
      value={value}
      onChange={(v?: string) => onChange?.((v ?? "").toUpperCase())}
      options={OPTIONS}
      filterOption={(input, option) => ((option as { search?: string } | undefined)?.search ?? "").includes(input.toLowerCase())}
      placeholder="Pick or type a ticker, e.g. AAPL"
      popupMatchSelectWidth={360}
      listHeight={360}
      allowClear
    />
  );
}
