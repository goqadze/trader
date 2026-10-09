import { BellOutlined, SoundOutlined } from "@ant-design/icons";
import { App as AntApp, Badge, Button, Divider, Empty, Popover, Space, Switch, Tooltip, Typography } from "antd";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { localTime } from "../trading/format";
import { tradingApi } from "../trading/api";
import { type AlertPrefs, KIND_LOOK, announce, askNotifyPermission, loadPrefs, savePrefs, title, unlockSound } from "./alerts";
import type { DipSignal } from "./types";

const MUTED = "#8b98b5";
const POLL_MS = 30_000;
const SHOWN = 3; // signals announced one by one per poll; more are summed up

/** One signal in a list: what happened, what it means, what the bot did. */
export function SignalLine({ s, showTime = true }: { s: DipSignal; showTime?: boolean }) {
  return (
    <div style={{ borderLeft: `3px solid ${KIND_LOOK[s.kind].color}`, paddingLeft: 8, marginBottom: 8 }}>
      <div>
        <b>{title(s)}</b>
        {showTime && <span style={{ color: MUTED, fontSize: 12 }}> · {localTime(s.created_at)}</span>}
      </div>
      <div style={{ fontSize: 12 }}>{s.message}</div>
      {s.outcome && <div style={{ fontSize: 12, color: MUTED }}>{s.outcome}</div>}
    </div>
  );
}

/** The header's bell: the dip bots' latest signals, and how you want to hear about new ones (a pop-up in the app, a
 *  desktop notification, a sound, the signal read aloud). It keeps checking every 30 s while the dashboard is open,
 *  even in a background tab, so the alerts reach you while you do something else. */
export default function SignalBell() {
  const { notification } = AntApp.useApp();
  const [prefs, setPrefs] = useState<AlertPrefs & { popup: boolean }>(() => ({ popup: true, ...loadPrefs() }));
  const prefsRef = useRef(prefs);
  prefsRef.current = prefs;
  const [items, setItems] = useState<DipSignal[]>([]);
  const [unseen, setUnseen] = useState(0);
  const lastId = useRef<number | null>(null);

  useEffect(() => {
    const poll = async () => {
      try {
        if (lastId.current == null) {
          // The first look only remembers where we are: old signals aren't announced again on every page load
          const latest = await tradingApi.signals(0, 20);
          setItems(latest);
          lastId.current = latest[0]?.id ?? 0;
          return;
        }
        const fresh = await tradingApi.signals(lastId.current, 50);
        if (!fresh.length) return;
        lastId.current = fresh[0].id;
        const ids = new Set(fresh.map((s) => s.id));
        setItems((prev) => [...fresh, ...prev.filter((p) => !ids.has(p.id))].slice(0, 30));
        setUnseen((n) => n + fresh.length);
        const oldestFirst = [...fresh].reverse();
        for (const s of oldestFirst.slice(-SHOWN)) {
          announce(s, prefsRef.current);
          if (prefsRef.current.popup) {
            notification.open({
              message: title(s),
              description: <span style={{ fontSize: 12 }}>{s.message} <span style={{ color: MUTED }}>{s.outcome}</span></span>,
              placement: "bottomRight",
              duration: 10,
            });
          }
        }
        if (oldestFirst.length > SHOWN && prefsRef.current.popup) {
          notification.info({ message: `${oldestFirst.length - SHOWN} more dip signals`, placement: "bottomRight" });
        }
      } catch {
        /* the trading service is down or restarting: try again next time */
      }
    };
    void poll();
    const t = setInterval(poll, POLL_MS);
    return () => clearInterval(t);
  }, [notification]);

  const update = async (change: Partial<AlertPrefs & { popup: boolean }>) => {
    if (change.notify && !(await askNotifyPermission())) {
      notification.warning({ message: "Desktop notifications are blocked", description: "Allow notifications for this site in the browser's settings, then switch this on again." });
      return;
    }
    if (change.sound || change.speak) unlockSound(); // browsers only allow sound after a click: this is it
    const next = { ...prefs, ...change };
    setPrefs(next);
    savePrefs(next);
  };

  const test = () => {
    unlockSound();
    const sample: DipSignal = {
      id: 0, bot_id: 0, created_at: new Date().toISOString(), symbol: "TEST", kind: "down", price: 94, reference_price: 100,
      change_pct: -6, message: "TEST is 6.0% under its 5-day high $100.00 at $94.00: buy zone", outcome: "This is only a test.",
    };
    announce(sample, prefs);
    if (prefs.popup) notification.open({ message: title(sample), description: sample.message, placement: "bottomRight" });
  };

  const content = (
    <div style={{ width: "min(380px, calc(100vw - 80px))" }}>
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <Space style={{ justifyContent: "space-between", width: "100%" }}>
          <span>Pop-ups in the app</span>
          <Switch size="small" checked={prefs.popup} onChange={(v) => update({ popup: v })} />
        </Space>
        <Space style={{ justifyContent: "space-between", width: "100%" }}>
          <Tooltip title="Shows up even when this tab is in the background (the browser asks once)"><span>Desktop notifications</span></Tooltip>
          <Switch size="small" checked={prefs.notify} onChange={(v) => update({ notify: v })} />
        </Space>
        <Space style={{ justifyContent: "space-between", width: "100%" }}>
          <Tooltip title="Falling tones for a dip, rising for a recovery, low for a stop-loss"><span>Sound</span></Tooltip>
          <Switch size="small" checked={prefs.sound} onChange={(v) => update({ sound: v })} />
        </Space>
        <Space style={{ justifyContent: "space-between", width: "100%" }}>
          <Tooltip title={'Reads each signal out loud, e.g. "AAPL is down 6.4 percent. Buy zone."'}><span>Read aloud</span></Tooltip>
          <Switch size="small" checked={prefs.speak} onChange={(v) => update({ speak: v })} />
        </Space>
        <Button size="small" icon={<SoundOutlined />} onClick={test}>Test an alert</Button>
      </Space>
      <Divider style={{ margin: "10px 0" }} />
      <div style={{ maxHeight: 360, overflowY: "auto" }}>
        {items.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span style={{ color: MUTED }}>No signals yet. A dip bot posts one when a symbol falls into its buy zone or gets back up.</span>} />
        ) : (
          items.slice(0, 12).map((s) => <SignalLine key={s.id} s={s} />)
        )}
      </div>
      <Typography.Text style={{ fontSize: 12, color: MUTED }}>
        For your information, not financial advice. Checked every 30 s while this dashboard is open. <Link to="/dip">Dip buyer</Link>
        {" · "}<Link to="/alerts">Email alerts</Link> (even with the dashboard closed)
      </Typography.Text>
    </div>
  );

  return (
    <Popover content={content} title="Dip signals" trigger="click" placement="bottomRight" onOpenChange={(open) => open && setUnseen(0)}>
      <Badge count={unseen} size="small" offset={[-6, 6]}>
        <Button type="text" icon={<BellOutlined />} style={{ color: prefs.notify || prefs.sound || prefs.speak ? "#4f8cff" : "#8b98b5" }} aria-label="Dip signals" />
      </Badge>
    </Popover>
  );
}
