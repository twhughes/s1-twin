// drawers/settings.js — the Settings drawer (DIRECTION rule 3: the settings menu and MIDI live in a
// drawer, off the plate). The parameters core/layout.js sends here (Fine tune, wheels and bend, chord
// voices, the MIDI messages, and any parameter nobody planned for), then the cockpit itself: sending
// the patch, Solo or Logic mode, the S-1's audio through this Mac, and the keyboards.
// The shell calls mount(el, ctx) once, then open() / close() as the drawer slides in and out.

import { knob } from "../design/knob.js";
import { seg } from "../design/seg.js";
import { plateLayout } from "../core/layout.js";
import { buildControl, bindControls } from "../core/controls.js";
import { sendPatch } from "../core/actions.js";

export const id = "settings";
export const title = "Settings";

let ctx = null;
let ui = null;
const VELOCITY_KEY = "synth.keyVelocity";

const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  e.append(...kids.filter((k) => k != null));
  return e;
};

export function mount(el, context) {
  ctx = context;
  ui = {};
  el.replaceChildren(
    h("button", { class: "quiet close", type: "button", "data-close": true, text: "Close" }),
    h("h2", { text: "Settings" }),
  );
  el.append(...paramSections());
  if (ctx.server) el.append(...s1Section(), ...monitorSection());
  el.append(...keyboardSection());
  ctx.on("status", render);
  render();
}

export function open() { render(); }
export function close() {}

// ── the parameters that live here ────────────────────────────────────────────
function paramSections() {
  const controls = new Map();
  const out = [];
  plateLayout(ctx.schema).settings.forEach((group, i) => {
    out.push(h("h3", { class: i === 0 ? "first" : null, text: group.title }));
    if (group.note) out.push(h("p", { text: group.note }));
    for (const r of group.rows) {
      const row = h("div", { class: r.wide ? "row wide" : "row" });
      for (const it of r.items) {
        const cells = it.type === "pair" ? it.items : [it];
        const holder = it.type === "pair" ? h("div", { class: "pair" }) : row;
        for (const c of cells) {
          const control = buildControl(c.spec, ctx);
          controls.set(c.cc, control);
          holder.append(control.el);
        }
        if (holder !== row) row.append(holder);
      }
      out.push(row);
    }
  });
  bindControls(ctx, controls);
  return out;
}

// ── the S-1: send the patch, and who runs the session ─────────────────────────
function s1Section() {
  ui.linkNote = h("p", { role: "status" });
  ui.send = h("button", { class: "pill", type: "button", text: "Send every setting to the S-1", onclick: async () => {
    ui.send.disabled = true;
    await sendPatch(ctx);
    ui.send.disabled = false;
  } });
  ui.mode = seg({
    label: "Mode",
    options: [{ value: "solo", label: "Solo" }, { value: "logic", label: "Logic" }],
    value: ctx.status.mode || "solo",
    onInput: async (mode) => {
      try {
        const r = await ctx.server.api("POST", "/api/mode", { mode });
        ctx._setStatus({ mode: r.mode });
      } catch (e) {
        ui.mode.set(ctx.status.mode);
        ctx.toast(`The mode did not change: ${e.message}`);
      }
    },
  });
  ui.modeNote = h("p", { class: "after" });
  return [
    h("h3", { text: "The S-1" }),
    ui.linkNote,
    h("div", { class: "saverow" }, ui.send),
    h("div", { class: "row spaced" }, ui.mode.el),
    ui.modeNote,
  ];
}

// ── the S-1's audio through this Mac ─────────────────────────────────────────
function monitorSection() {
  ui.monNote = h("p", { role: "status" });
  ui.mute = seg({
    label: "Monitor",
    options: [{ value: 0, label: "On" }, { value: 1, label: "Muted" }],
    value: ctx.status.monitor && ctx.status.monitor.muted ? 1 : 0,
    onInput: async (v) => {
      try {
        const r = await ctx.server.api("POST", "/api/monitor/mute", { muted: v === 1 });
        ctx._setStatus({ monitor: r });
      } catch (e) {
        ctx.toast(`The monitor did not change: ${e.message}`);
      }
    },
  });
  let gainTimer = 0;
  ui.gain = knob({
    label: "Level", min: 0, max: 40, def: 10,
    value: Math.round(((ctx.status.monitor && ctx.status.monitor.gain) ?? 1) * 10),
    format: (v) => `×${(v / 10).toFixed(1)}`,
    onInput: (v) => {
      clearTimeout(gainTimer);
      gainTimer = setTimeout(() => ctx.server.api("POST", "/api/monitor/gain", { gain: v / 10 })
        .catch((e) => ctx.toast(`The level did not change: ${e.message}`)), 120);
    },
  });
  ui.meterFill = h("i");
  ui.meterText = h("span", { class: "meter-text" });
  return [
    h("h3", { text: "Sound from the S-1" }),
    ui.monNote,
    h("div", { class: "row spaced" }, ui.mute.el, ui.gain.el,
      h("div", { class: "meter-wrap" }, h("div", { class: "meter", role: "img", "aria-label": "Peak level" }, ui.meterFill), ui.meterText)),
  ];
}

// ── keyboards ────────────────────────────────────────────────────────────────
function keyboardSection() {
  const keys = ctx.keys;
  ui.kbNote = h("p", { role: "status" });
  ui.midiNote = h("p");
  ui.midiBtn = h("button", { class: "linkish", type: "button", text: "Use a MIDI keyboard in this browser", onclick: async () => {
    try {
      const names = await keys.midi.enable();
      ctx.toast(names.length ? `Listening to ${names.join(", ")}.` : "No MIDI keyboard found. Plug one in; it appears here by itself.");
    } catch (e) {
      ctx.toast(e && e.name === "SecurityError" ? "The browser said no to MIDI. Allow it in the site settings to try again." : (e.message || "Web MIDI did not start."));
    }
    render();
  } });
  let saved = null;
  try { saved = Number(localStorage.getItem(VELOCITY_KEY)) || null; } catch { /* private mode */ }
  if (keys && saved) keys.velocity = saved;
  ui.velocity = knob({
    label: "Key velocity", min: 1, max: 127, def: 100, value: keys ? keys.velocity : 100,
    onInput: (v) => {
      if (keys) keys.velocity = v;
      try { localStorage.setItem(VELOCITY_KEY, String(v)); } catch { /* private mode */ }
    },
  });
  if (keys) keys.onChange(render);
  return [
    h("h3", { text: "Keyboards" }),
    ui.kbNote,
    keys && keys.midi.supported ? h("p", {}, ui.midiBtn) : null,
    ui.midiNote,
    h("div", { class: "row" }, ui.velocity.el),
    h("p", { class: "after", text: "Key velocity sets how hard the computer keys and the screen keys play." }),
  ];
}

// ── words that follow the status ─────────────────────────────────────────────
function render() {
  if (!ui) return;
  const st = ctx.status;
  if (ui.linkNote) {
    ui.linkNote.textContent = st.demo ? "Demo mode: the S-1 here is pretend, and nothing is sent."
      : st.link === "synced" ? `In sync with the S-1 on ${st.port}. Every knob moves both ways.`
        : st.link === "listening" ? `The S-1 is connected on ${st.port}. Knob moves flow both ways, but the S-1 keeps its own patch until you send this one.`
          : st.link === "connecting" ? "Connecting to the S-1."
            : "The S-1 is not connected. Plug it in with a USB cable; this app finds it by itself.";
    ui.send.disabled = !(st.link === "listening" || st.link === "synced") || Boolean(st.demo);
  }
  if (ui.mode) {
    ui.mode.set(st.mode || "solo");
    ui.modeNote.textContent = st.mode === "logic"
      ? "Logic: Logic Pro owns the notes, the clock and the S-1's audio. This app still syncs every knob, the library and Save to S-1."
      : "Solo: this app forwards your MIDI keyboard to the S-1, plays its audio through this Mac, and sends the MIDI clock.";
  }
  if (ui.monNote) {
    const m = st.monitor || {};
    ui.monNote.textContent = st.mode === "logic" ? "In Logic mode, Logic Pro plays the S-1's audio."
      : m.running ? (m.muted ? "The S-1's audio reaches this Mac and is muted." : "The S-1's audio plays through this Mac.")
        : "No audio from the S-1 yet. It starts by itself when the S-1's USB audio appears.";
    ui.mute.set(m.muted ? 1 : 0);
    if (typeof m.gain === "number" && !ui.gain.el.contains(document.activeElement)) ui.gain.set(Math.round(m.gain * 10));
    const db = m.running && typeof m.peak_db === "number" ? m.peak_db : -120;
    ui.meterFill.style.width = `${Math.max(0, Math.min(100, ((db + 60) / 60) * 100))}%`;
    ui.meterText.textContent = m.running ? `${Math.round(db)} dB peak` : "no signal";
  }
  const names = st.keyboards || [];
  ui.kbNote.textContent = !ctx.server ? "Play with the computer keys, the screen keys, or a MIDI keyboard."
    : names.length ? `${names.join(", ")} ${names.length > 1 ? "play" : "plays"} the S-1 directly (in Solo mode).`
      : "No MIDI keyboard is plugged in. One that is plugged in plays the S-1 directly (in Solo mode).";
  if (ctx.keys) {
    const m = ctx.keys.midi;
    ui.midiNote.textContent = m.enabled
      ? (m.names().length ? `This browser hears ${m.names().join(", ")}. With no S-1 connected, it plays the twin.` : "This browser hears no MIDI keyboard yet.")
      : "";
    ui.midiBtn.hidden = m.enabled;
  }
}
