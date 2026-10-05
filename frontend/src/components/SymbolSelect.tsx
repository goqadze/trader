import { AutoComplete } from "antd";
import { useMemo } from "react";
import { SYMBOL_GROUPS, symbolOptions, type SymbolGroup } from "../symbols";

const MUTED = "#8b98b5";

const optionsFor = (groups: SymbolGroup[]) => symbolOptions((s) => (
  <span>
    <b>{s.symbol}</b> <span style={{ color: MUTED }}>{s.name}</span>
  </span>
), groups).map((g) => ({ ...g, options: g.options.map((o) => ({ ...o, search: `${o.value} ${o.name}`.toLowerCase() })) }));

interface Props {
  value?: string;
  onChange?: (value: string) => void;
  groups?: SymbolGroup[]; // the suggested groups (default: all)
}

// Symbol field: a dropdown of suggested tickers that still takes any typed ticker. Typing filters the
// suggestions by ticker or name; whatever is in the box when the form submits is the symbol.
// value/onChange come from the surrounding Form.Item.
export default function SymbolSelect({ value, onChange, groups = SYMBOL_GROUPS }: Props) {
  const options = useMemo(() => optionsFor(groups), [groups]);
  return (
    <AutoComplete
      value={value}
      onChange={(v?: string) => onChange?.((v ?? "").toUpperCase())}
      options={options}
      filterOption={(input, option) => ((option as { search?: string } | undefined)?.search ?? "").includes(input.toLowerCase())}
      placeholder="Pick or type a ticker, e.g. AAPL"
      popupMatchSelectWidth={360}
      listHeight={360}
      allowClear
    />
  );
}
