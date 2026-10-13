// Saved setups of a strategy (the dip buyer's, the momentum rotation's): save the backtest form's rules, symbols,
// capital and periods under a name you pick, and pick it again later to fill a form with it (the backtest, a new bot,
// a running dip bot's rules). trading-service keeps them (/dip/presets, /rotation/presets), so they survive a cleared
// browser and show in every browser. What a strategy's setup is comes from its kit (dip/kit.tsx, rotation/kit.tsx).

import { DeleteOutlined, SaveOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Input, type InputRef, Modal, Popconfirm, Select, Space, Tooltip, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import type { StrategyKit } from "./kit";
import type { SavedSetup } from "./types";

const MUTED = "#8b98b5";

interface Props<C extends { symbols: string[] }> {
  kit: StrategyKit<C>;
  /** Fill the form with this setup (every rule is there: one added since it was saved gets its default). */
  onLoad: (c: C) => void;
  /** The form's values as a setup right now, to say when they differ from the loaded one. */
  values?: C | null;
  /** Check the form and return its setup. Given: the setups can be saved and deleted here; left out: only loaded. */
  validate?: () => Promise<C>;
}

export default function SetupPicker<C extends { symbols: string[] }>({ kit, onLoad, values, validate }: Props<C>) {
  const { message } = AntApp.useApp();
  const { canon } = kit;
  const [presets, setPresets] = useState<SavedSetup<C>[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [pending, setPending] = useState<C | null>(null); // the setup being saved (the dialog is open)
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const nameRef = useRef<InputRef>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await kit.presets.list();
      setPresets(list);
      setSelected((id) => (list.some((p) => p.id === id) ? id : null)); // deleted elsewhere
      setLoadError(null);
      return list;
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [kit]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const sel = presets?.find((p) => p.id === selected) ?? null;
  const edited = !!sel && !!values && canon(values) !== canon(sel.config);
  const clash = pending ? presets?.find((p) => p.name.toLowerCase() === name.trim().toLowerCase()) : undefined;

  // On select, not on change: picking the loaded one again (to undo your changes) loads it again
  const pick = (id: number) => {
    setSelected(id);
    const p = presets?.find((x) => x.id === id);
    if (p) onLoad(kit.complete(p.config));
  };

  const openSave = async () => {
    if (!validate) return;
    try {
      setPending(await validate());
    } catch {
      message.warning("Fix the highlighted fields first");
      return;
    }
    setName(sel?.name ?? "");
  };

  const save = async () => {
    const n = name.trim();
    if (!n || !pending) return;
    setSaving(true);
    try {
      const p = await kit.presets.save(n, pending);
      await refresh();
      setSelected(p.id);
      if (canon(p.config) !== canon(pending)) onLoad(kit.complete(p.config)); // tidied up (e.g. tickers)
      setPending(null);
      message.success(`Saved “${p.name}”`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!sel) return;
    try {
      await kit.presets.remove(sel.id);
      setSelected(null);
      await refresh();
      message.success(`Deleted “${sel.name}”`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <Space.Compact style={{ width: "100%" }}>
        <Select<number>
          style={{ flex: "1 1 auto", minWidth: 0 }}
          placeholder={loadError ? "Saved setups unavailable" : "Pick a saved setup"}
          value={selected ?? undefined}
          onSelect={pick}
          onOpenChange={(open) => open && void refresh()} // setups saved since (another tab, another form) show up too
          allowClear
          onClear={() => setSelected(null)}
          showSearch
          optionFilterProp="label"
          loading={presets === null && !loadError}
          notFoundContent={
            <span style={{ color: MUTED, fontSize: 12 }}>
              {loadError ? `Can't reach trading-service: ${loadError}` : validate ? "Nothing saved yet: set the rules you like, then Save" : "Nothing saved yet: save one on the Backtest tab"}
            </span>
          }
          options={(presets ?? []).map((p) => ({ value: p.id, label: p.name, summary: kit.summary(p.config) }))}
          optionRender={(o) => (
            <div>
              <div>{o.label}</div>
              <div style={{ fontSize: 11, color: MUTED }}>{o.data.summary}</div>
            </div>
          )}
        />
        {validate && (
          <Tooltip title={`Save these settings under a name: ${kit.keeps}`}>
            <Button icon={<SaveOutlined />} onClick={openSave} disabled={!!loadError}>Save</Button>
          </Tooltip>
        )}
        {validate && sel && (
          <Popconfirm title={`Delete the saved “${sel.name}”?`} okText="Delete" okButtonProps={{ danger: true }} onConfirm={remove}>
            <Tooltip title={`Delete “${sel.name}”`}>
              <Button icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        )}
      </Space.Compact>
      {edited && (
        <Typography.Text style={{ display: "block", fontSize: 12, color: "#d4a72c", marginTop: 4 }}>
          Changed since you loaded “{sel.name}”{validate ? ": Save to keep the changes" : ""}
        </Typography.Text>
      )}
      <Modal
        title="Save this setup"
        open={!!pending}
        onCancel={() => setPending(null)}
        onOk={save}
        okText={clash ? "Replace" : "Save"}
        okButtonProps={{ loading: saving, disabled: !name.trim() }}
        afterOpenChange={(open) => open && nameRef.current?.focus({ cursor: "all" })}
        width={420}
        destroyOnClose
      >
        <Input ref={nameRef} value={name} onChange={(e) => setName(e.target.value)} onPressEnter={save} maxLength={60} placeholder="Name, e.g. Sector dips 7%" />
        <Typography.Text style={{ display: "block", fontSize: 12, color: clash ? "#d4a72c" : MUTED, marginTop: 8 }}>
          {clash
            ? `Replaces the saved “${clash.name}”.`
            : `Keeps ${kit.keeps}. Pick it from the list later to fill all of them in.`}
        </Typography.Text>
      </Modal>
    </>
  );
}
