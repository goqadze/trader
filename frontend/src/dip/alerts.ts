// How a dip bot's signals reach you while the dashboard is open: a desktop notification, a short sound (falling tones for
// "down", rising for "up"), and/or the signal read aloud. Your choice is remembered in this browser only.

import type { DipSignal } from "./types";

export interface AlertPrefs {
  notify: boolean; // desktop notifications (the browser asks for permission once)
  sound: boolean;
  speak: boolean; // read each signal aloud
}

const KEY = "dip:alerts";
const OFF: AlertPrefs = { notify: false, sound: false, speak: false };

export function loadPrefs(): AlertPrefs {
  try {
    return { ...OFF, ...JSON.parse(localStorage.getItem(KEY) ?? "{}") };
  } catch {
    return OFF; // private window or blocked storage: alerts start off
  }
}

export function savePrefs(p: AlertPrefs) {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    /* not remembered, but still works for this visit */
  }
}

export const KIND_LOOK: Record<DipSignal["kind"], { icon: string; color: string; label: string }> = {
  down: { icon: "⬇", color: "#4f8cff", label: "Buy zone" },
  rebound: { icon: "↗", color: "#13c2c2", label: "Turned up" },
  up: { icon: "⬆", color: "#33c088", label: "Back up" },
  stop: { icon: "⛔", color: "#ef5b6b", label: "Stop-loss" },
  time: { icon: "⏱", color: "#d4a72c", label: "Held too long" },
  news: { icon: "📰", color: "#d4a72c", label: "Bearish news" },
};

export function title(s: DipSignal): string {
  const change = s.change_pct == null ? "" : ` ${s.change_pct > 0 ? "+" : ""}${s.change_pct.toFixed(1)}%`;
  switch (s.kind) {
    case "down":
      return `${KIND_LOOK.down.icon} ${s.symbol}${change}: buy zone`;
    case "rebound":
      return `${KIND_LOOK.rebound.icon} ${s.symbol} turned up from its low: buy`;
    case "up":
      return `${KIND_LOOK.up.icon} ${s.symbol} is back up${change}: sell`;
    case "stop":
      return `${KIND_LOOK.stop.icon} ${s.symbol} hit its stop${change}: sold, blacklisted`;
    case "time":
      return `${KIND_LOOK.time.icon} ${s.symbol} held too long${change}: sold`;
    default:
      return `${KIND_LOOK.news.icon} ${s.symbol}: bearish news, not bought`;
  }
}

function spoken(s: DipSignal): string {
  const pctWords = s.change_pct == null ? "" : ` ${Math.abs(s.change_pct).toFixed(1)} percent`;
  switch (s.kind) {
    case "down":
      return `${s.symbol} is down${pctWords}. Buy zone.`;
    case "rebound":
      return `${s.symbol} turned up from its low. Buy.`;
    case "up":
      return `${s.symbol} is back up. Sell.`;
    case "stop":
      return `${s.symbol} hit its stop loss, down${pctWords}. Sold and blacklisted.`;
    case "time":
      return `${s.symbol} was held too long. Sold.`;
    default:
      return `${s.symbol} has bearish news. Not bought today.`;
  }
}

let audio: AudioContext | null = null;

/** Create the audio context. Browsers allow it only after a click, so call this from the toggle's click. */
export function unlockSound() {
  try {
    audio ??= new AudioContext();
    void audio.resume();
  } catch {
    audio = null;
  }
}

/** Two short tones: falling for a dip, rising for a recovery, low for a stop. */
export function chime(kind: DipSignal["kind"]) {
  if (!audio) return;
  const notes = kind === "down" ? [880, 587] : kind === "up" || kind === "rebound" ? [587, 880] : kind === "stop" ? [330, 247] : [660, 660];
  const t0 = audio.currentTime;
  notes.forEach((freq, i) => {
    const osc = audio!.createOscillator();
    const gain = audio!.createGain();
    osc.frequency.value = freq;
    osc.type = "sine";
    gain.gain.setValueAtTime(0.0001, t0 + i * 0.18);
    gain.gain.exponentialRampToValueAtTime(0.25, t0 + i * 0.18 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.18 + 0.16);
    osc.connect(gain).connect(audio!.destination);
    osc.start(t0 + i * 0.18);
    osc.stop(t0 + i * 0.18 + 0.17);
  });
}

export async function askNotifyPermission(): Promise<boolean> {
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  return (await Notification.requestPermission()) === "granted";
}

/** Tell the user about one signal, the ways they chose. */
export function announce(s: DipSignal, prefs: AlertPrefs) {
  if (prefs.notify && "Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(title(s), { body: `${s.message}\n${s.outcome}`, tag: `dip-signal-${s.id}` });
    } catch {
      /* some browsers only allow notifications from a service worker: the in-page list still shows it */
    }
  }
  if (prefs.sound) chime(s.kind);
  if (prefs.speak && "speechSynthesis" in window) {
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(spoken(s)));
  }
}
