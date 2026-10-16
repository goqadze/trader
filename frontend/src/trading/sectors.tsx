// Sector badges: which parts of the market a bot trades (trading-service sectors.py). Worked out from the bot's
// symbols until set by hand here; shown in the bot table (filterable) and on the bot's page.

import { EditOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Modal, Select, Space, Tag, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { tradingApi } from "./api";
import type { Bot } from "./types";

const MUTED = "#8b98b5";

// A color per badge, so a theme is spotted down the table; names set by hand get the plain tag
const COLOR: Record<string, string> = {
  Chips: "geekblue", Tech: "blue", Software: "cyan", Energy: "volcano", Nuclear: "lime", Utilities: "green",
  "Power & grid": "gold", "Metals & mining": "orange", Commodities: "orange", Healthcare: "magenta", Defensive: "green",
  Financials: "purple", Crypto: "gold", Consumer: "magenta", "Real estate": "cyan",
};

export function SectorTags({ sectors, empty = "—" }: { sectors: string[]; empty?: string }) {
  if (!sectors.length) return <span style={{ color: MUTED }}>{empty}</span>;
  return (
    <Space size={4} wrap>
      {sectors.map((s) => <Tag key={s} color={COLOR[s]} style={{ marginInlineEnd: 0 }}>{s}</Tag>)}
    </Space>
  );
}

/** The bot page's badges with an edit button: pick or type up to 6, none, or back to automatic. */
export function SectorBadges({ bot, onChanged }: { bot: Bot; onChanged: () => void }) {
  const { message } = AntApp.useApp();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<string[]>([]);
  const [options, setOptions] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState("");
  // The listed badges first, so "Commod" + Enter picks Commodities; a name of your own comes last, as "Add …"
  const known = [...new Set([...options, ...value])];
  const typed = search.trim().replace(/\s+/g, " ");
  const choices = [
    ...known.map((o) => ({ label: o, value: o })),
    ...(typed && !known.some((o) => o.toLowerCase() === typed.toLowerCase()) ? [{ label: `Add “${typed}”`, value: typed }] : []),
  ];

  useEffect(() => {
    if (!open) return;
    setValue(bot.sectors);
    setSearch("");
    tradingApi.sectors().then(setOptions).catch(() => setOptions([]));
  }, [open]); // the bot's badges as the dialog opens, not each poll's

  const save = async (sectors: string[] | null) => {
    setSaving(true);
    try {
      await tradingApi.setSectors(bot.id, sectors);
      message.success(sectors === null ? "Sector badges: automatic again" : "Sector badges saved");
      setOpen(false);
      onChanged();
    } catch (e) {
      message.error(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
      {bot.sectors.length > 0 && <SectorTags sectors={bot.sectors} />}
      <Tooltip title={bot.sectors.length
        ? `Sector badges, ${bot.sectors_auto ? "worked out from its symbols" : "set by hand"}: click to change`
        : "No sector badge: click to add one"}>
        <Button size="small" type="text" icon={<EditOutlined />} aria-label="Edit sector badges" onClick={() => setOpen(true)}
          style={{ color: MUTED }}>
          {bot.sectors.length ? null : "Sectors"}
        </Button>
      </Tooltip>
      <Modal
        title="Sector badges"
        open={open}
        onCancel={() => setOpen(false)}
        footer={[
          <Button key="auto" disabled={saving || bot.sectors_auto} onClick={() => save(null)}>Back to automatic</Button>,
          <Button key="cancel" onClick={() => setOpen(false)}>Cancel</Button>,
          <Button key="save" type="primary" loading={saving} onClick={() => save(value)}>Save</Button>,
        ]}
      >
        <p style={{ color: MUTED, marginTop: 0 }}>
          Which parts of the market this bot trades, shown in the bot table. Only a label: how the bot trades doesn't
          change. Pick from the list or type your own (up to 6).
        </p>
        <Select
          mode="multiple"
          style={{ width: "100%" }}
          value={value}
          onChange={(v) => { setValue(v); setSearch(""); }}
          maxCount={6}
          placeholder="No badge"
          searchValue={search}
          onSearch={setSearch}
          filterOption={(input, o) => String(o?.value).toLowerCase().includes(input.trim().replace(/\s+/g, " ").toLowerCase())}
          options={choices}
        />
        <div style={{ color: MUTED, marginTop: 12 }}>
          {bot.sectors_auto ? "Now automatic: " : "Set by hand. Automatic would be: "}
          <SectorTags sectors={bot.sectors_from_symbols} empty="none (its symbols' sectors are unknown)" />
          <div style={{ marginTop: 6 }}>
            Automatic: a sector with at least a fifth of the bot's symbols is a badge (three at most); a list spread
            thinner is Diversified.
          </div>
        </div>
      </Modal>
    </span>
  );
}
