// views/sequencer.js — the Sequencer view: piano roll, transport, performance, banks, S-1 patterns.
// Contract: docs/design/BUILD.md §2.2 (ctx) and ROUND2.md §2. The page's one transport
// (ctx.transport, core/transport.js) owns the sequence, the performance settings and the engine, so a
// pattern keeps playing on the other views; this view is its UI. It edits ctx.transport.seq and calls
// edited(); the transport sends the edits to the cockpit (/api/sequence, /api/transport) and follows it,
// or runs its own clock on the static page. The banks and the S-1's patterns (/api/sequences,
// /api/device/pattern) are this view's own. Space, ⇧ Space, − and = and Delete come from
// core/shortcuts.js; the roll keeps its own keys while it has focus.
// The pure helpers (rollGeometry, cellAt, moveCursor, …) run in node: views/sequencer.check.mjs; the
// step rules (stepSeconds, stepPlan, swingDelay, …) moved to core/transport.js with the engine.

import { knob } from "../design/knob.js";
import { seg } from "../design/seg.js";
import * as draw from "../design/draw.js";
import { rgbOf, rgba, noteName } from "../design/colors.js";
import { MAX_STEPS, stepSeconds, clampDuration, createTransport } from "../core/transport.js";

export const id = "sequencer";
export const title = "Sequencer";

// ── pure helpers ─────────────────────────────────────────────────────────────────────
export const GRIDS = [
  { value: "1/8", label: "1/8" }, { value: "1/16", label: "1/16" }, { value: "1/32", label: "1/32" },
  { value: "8t", label: "8t" }, { value: "16t", label: "16t" }, { value: "32t", label: "32t" },
];
const BLACK = new Set([1, 3, 6, 8, 10]);

/** How many steps make one beat on this grid (for the stronger grid line): 4 on 1/16, 3 on 8t. */
export function stepsPerBeat(res) {
  const beatsPerStep = stepSeconds(60, res);                 // at 60 BPM a beat is one second
  return Math.max(1, Math.round(1 / beatsPerStep));
}

/** The note sounding at (step, pitch): one that starts there or holds over it. */
export function noteAt(notes, step, pitch) {
  return notes.find((n) => n.pitch === pitch && step >= n.step && step < n.step + n.duration) || null;
}

/** Roll layout for `steps` columns in a well `width` px wide. Rows run from `hi` (top) to `lo`. */
export function rollGeometry({ steps = 16, width = 800, lo = 24, hi = 96, rowH = 14, labelW = 44, minCell = 18, maxCell = 96 } = {}) {
  const n = Math.max(1, Math.min(MAX_STEPS, steps | 0));
  const cellW = Math.max(minCell, Math.min(maxCell, Math.floor((width - labelW) / n)));
  const spare = width - labelW - n * cellW;          // the rounding remainder goes to the key strip,
  if (cellW < maxCell && spare > 0) labelW += spare;  // so the grid meets the well's right edge
  const rows = hi - lo + 1;
  return { steps: n, cellW, rowH, labelW, lo, hi, rows, w: labelW + n * cellW, h: rows * rowH };
}
export const rowY = (g, pitch) => (g.hi - pitch) * g.rowH;
export const stepX = (g, step) => g.labelW + step * g.cellW;

/** The cell under a point (CSS px inside the canvas), or null off the grid. */
export function cellAt(g, x, y) {
  if (x < g.labelW || y < 0) return null;
  const step = Math.floor((x - g.labelW) / g.cellW), pitch = g.hi - Math.floor(y / g.rowH);
  if (step < 0 || step >= g.steps || pitch < g.lo || pitch > g.hi) return null;
  return { step, pitch };
}

/** A note's rectangle (inset by a pixel so neighbours stay apart). */
export function noteRect(g, n) {
  return { x: stepX(g, n.step) + 1, y: rowY(g, n.pitch) + 1, w: n.duration * g.cellW - 2, h: g.rowH - 2 };
}

/** Arrow-key movement of the roll's keyboard cursor. */
export function moveCursor(cur, key, { steps, lo, hi }, shift = false) {
  const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, shift ? 12 : 1], ArrowDown: [0, shift ? -12 : -1] }[key];
  if (!d) return cur;
  return { step: Math.max(0, Math.min(steps - 1, cur.step + d[0])), pitch: Math.max(lo, Math.min(hi, cur.pitch + d[1])) };
}

/** A bank name the app accepts (synth/patches.py sanitize_name): no path separators, no leading dot. */
export const validName = (name) => {
  const n = String(name || "").trim();
  return !!n && n !== "." && n !== ".." && !/[\\/\u0000]/.test(n) && !n.startsWith(".");
};

/** Position words: "Step 5 of 16", "Paused at step 5", "Stopped" (the transport's state). */
export function positionText({ playing, paused, position, steps }) {
  if (paused && position >= 0) return `Paused at step ${position + 1}`;
  if (playing && position >= 0) return `Step ${position + 1} of ${steps}`;
  if (playing) return "Starting";
  return "Stopped";
}

export function warningText(steps) {
  if (!steps.length) return "";
  const list = steps.map((s) => s + 1);
  const words = list.length === 1 ? `Step ${list[0]} holds` : `Steps ${list.slice(0, -1).join(", ")} and ${list[list.length - 1]} hold`;
  return `${words} more than four notes. The S-1 plays four notes a step at most, so Save to S-1 drops the rest.`;
}

// ── the view ─────────────────────────────────────────────────────────────────────────
const CSS = `
.v-seq { position: relative; z-index: 1; max-width: 1480px; margin: 0 auto; padding: 30px var(--gutter) 56px; }
.v-seq .sq-bar { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 22px 40px; }
.v-seq .sq-group { display: flex; flex-direction: column; gap: 10px; }
.v-seq .sq-group > .k-label { color: var(--ink); }
.v-seq .sq-transport { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.v-seq .pill[aria-pressed="true"] { border-color: var(--ink); box-shadow: inset 0 0 0 0.75px var(--ink); }
.v-seq .sq-pos { min-width: 9.5em; color: var(--ink-2); font-size: 13.5px; font-variant-numeric: tabular-nums; margin-left: 6px; }
.v-seq .sq-fields { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 14px 26px; }
.v-seq .sq-field { display: flex; flex-direction: column; gap: 4px; }
.v-seq .sq-field input, .v-seq select, .v-seq .sq-save input {
  background: var(--deep); color: var(--ink); border: 1px solid var(--ink-3); border-radius: 3px;
  padding: 7px 9px; font: 400 14px var(--sans); font-variant-numeric: tabular-nums; }
.v-seq .sq-field input { width: 5.6em; }
.v-seq .sq-field input:focus, .v-seq select:focus, .v-seq .sq-save input:focus { border-color: var(--ink); outline: none; }
.v-seq .sq-field input:focus-visible, .v-seq select:focus-visible, .v-seq .sq-save input:focus-visible { outline: 1.5px dashed var(--ink); outline-offset: 3px; }
.v-seq .sq-perf { display: flex; gap: 10px; }
.v-seq .sq-warn { margin: 22px 0 0; font-size: 13.5px; color: var(--ink); max-width: 80ch; padding-left: 14px; border-left: 1.25px dashed var(--ink-3); }
.v-seq .sq-roll { margin-top: 26px; max-height: 440px; overflow: auto; overscroll-behavior: contain; }
.v-seq .sq-roll canvas { display: block; touch-action: none; cursor: crosshair; max-width: none; }
.v-seq .sq-roll canvas:focus-visible { outline-offset: -3px; }
.v-seq .sq-under { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 18px 40px; }
.v-seq .sq-under .caption { flex: 1 1 320px; }
.v-seq .sq-under .caption .note { max-width: 70ch; }
.v-seq .sq-inspector { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 18px; margin-top: 14px; }
.v-seq .sq-which { display: flex; align-items: center; gap: 9px; font-size: 14px; font-variant-numeric: tabular-nums; min-width: 9em; }
.v-seq .sq-swatch { width: 13px; height: 13px; border-radius: 50%; background: rgb(var(--pc)); box-shadow: 0 0 0 1.25px var(--ink); }
.v-seq .sq-lower { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--gap); margin-top: 44px; }
.v-seq .sq-lower .note { margin: 0 0 14px; }
.v-seq .sq-save { display: flex; gap: 10px; flex-wrap: wrap; }
.v-seq .sq-save input { flex: 1 1 12em; min-width: 0; }
.v-seq .sq-confirm { margin: 12px 0 0; font-size: 13.5px; color: var(--ink); display: flex; gap: 8px 16px; flex-wrap: wrap; align-items: baseline; }
.v-seq .sq-list { list-style: none; margin: 16px 0 0; padding: 0; }
.v-seq .sq-list li { display: flex; align-items: baseline; gap: 16px; padding: 7px 0; border-bottom: 1px solid var(--ink-4); }
.v-seq .sq-list li span { flex: 1; font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.v-seq .sq-pc { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px 18px; }
.v-seq .sq-pc label { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
.v-seq .sq-hidden { display: none !important; }
@media (max-width: 760px) {
  .v-seq .sq-lower { grid-template-columns: minmax(0, 1fr); }
  .v-seq .sq-roll { max-height: 360px; }
  .v-seq .sq-bar { gap: 18px 28px; }
}
`;

let current = null;

export function mount(root, ctx) {
  if (current) unmount();
  current = createView(root, ctx);
}

export function unmount() {
  if (!current) return;
  try { current.destroy(); } finally { current = null; }
}

/** The KeyHint bar on this view (app.js shows it): the transport's keys, then the roll's. */
// One row in the KeyHint bar: the roll's own keys are in the caption under the roll and in the ? list.
export const hints = [
  { key: "Space", label: "Play/pause" },
  { key: "⇧ Space", label: "Stop" },
  { key: "− =", label: "Tempo" },
  { key: "?", label: "Keys" },
];

export default { id, title, mount, unmount, hints };

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}

function createView(root, ctx) {
  const staticMode = !ctx.server;
  const api = (m, p, b) => ctx.server.api(m, p, b);
  const disposers = [];
  const timers = new Set();
  const later = (fn, ms) => { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); return t; };
  const style = h("style", { "data-view": "sequencer" });
  style.textContent = CSS;
  document.head.append(style);
  disposers.push(() => style.remove());

  // The page's transport owns the sequence and the engine; without the shell (a bare mount) the view
  // makes its own for its lifetime.
  const T = ctx.transport || createTransport(ctx);
  if (!ctx.transport) disposers.push(() => T.destroy());
  const seq = T.seq, perf = T.perf;            // the ONE copy: edit it, then push()
  let selected = null, drag = null;
  let cursor = { step: 0, pitch: 60 }, cursorOn = false;
  let newVelocity = 100;
  let scrolled = false;                         // the roll opened on the notes once
  let geo = rollGeometry({ steps: seq.steps });

  // ── layout ──────────────────────────────────────────────────────────────────────
  const view = h("section", { class: "v-seq", "aria-label": "Sequencer" });
  root.append(view);
  disposers.push(() => view.remove());

  const playBtn = h("button", { type: "button", class: "pill", "data-action": "play", "aria-pressed": "false", onclick: () => T.play() }, "Play");
  const pauseBtn = h("button", { type: "button", class: "pill", "data-action": "pause", "aria-pressed": "false", onclick: () => T.pause() }, "Pause");
  const stopBtn = h("button", { type: "button", class: "pill", "data-action": "stop", onclick: () => T.stop() }, "Stop");
  const pos = h("span", { class: "sq-pos", text: "Stopped" });
  const bpmIn = h("input", { type: "number", min: "20", max: "300", step: "0.5", value: String(seq.bpm), "aria-label": "Tempo in beats per minute" });
  const stepsIn = h("input", { type: "number", min: "1", max: String(MAX_STEPS), step: "1", value: String(seq.steps), "aria-label": "Pattern length in steps" });
  const gridSeg = seg({ label: "Grid", value: seq.step_resolution, options: GRIDS, onInput: (v) => { seq.step_resolution = v; drawRoll(); push(true); } });
  const clockSeg = seg({ label: "Send MIDI clock", value: perf.clock ? "on" : "off", options: [{ value: "off", label: "Off" }, { value: "on", label: "On" }],
    onInput: (v) => T.setPerf({ clock: v === "on" }) });
  const pct = (v) => `${v}%`;
  const gateKnob = knob({ label: "Gate", min: 5, max: 100, value: Math.round(perf.gate * 100), def: 100, format: pct, onInput: (v) => T.setPerf({ gate: v / 100 }) });
  const shuffleKnob = knob({ label: "Shuffle", min: 0, max: 75, value: Math.round(perf.shuffle * 100), def: 0, format: pct, onInput: (v) => T.setPerf({ shuffle: v / 100 }) });
  const probKnob = knob({ label: "Probability", min: 0, max: 100, value: Math.round(perf.probability * 100), def: 100, format: pct, onInput: (v) => T.setPerf({ probability: v / 100 }) });

  const warn = h("p", { class: "sq-warn sq-hidden", role: "status", "data-role": "poly-warning" });
  const canvas = h("canvas", { tabindex: "0", role: "application", "aria-roledescription": "piano roll",
    "aria-label": "Piano roll. Arrow keys move, Enter adds or selects a note, Delete removes it, [ and ] change its length." });
  const well = h("div", { class: "well sq-roll" }, canvas);

  const whichSwatch = h("span", { class: "sq-swatch", "aria-hidden": "true" });
  const whichText = h("span");
  const velKnob = knob({ label: "Velocity", min: 1, max: 127, value: 100, def: 100, onInput: (v) => {
    if (!selected) return; selected.velocity = v; newVelocity = v; drawRoll(); push(); } });
  const lenKnob = knob({ label: "Length", min: 1, max: MAX_STEPS, value: 1, def: 1, format: (v) => (v === 1 ? "1 step" : `${v} steps`), onInput: (v) => {
    if (!selected) return; selected.duration = clampDuration(v, selected.step, seq.steps); if (selected.duration !== v) lenKnob.set(selected.duration); drawRoll(); push(); } });
  const delNote = h("button", { type: "button", class: "quiet", "data-action": "delete-note", onclick: () => deleteNote(selected) }, "Delete note");
  const inspector = h("div", { class: "sq-inspector sq-hidden", "data-role": "inspector", "aria-label": "The selected note" },
    h("span", { class: "sq-which" }, whichSwatch, whichText), velKnob.el, lenKnob.el, delNote);

  view.append(
    h("div", { class: "sq-bar" },
      h("div", { class: "sq-group" },
        h("div", { class: "sq-transport", role: "group", "aria-label": "Transport" }, playBtn, pauseBtn, stopBtn, pos),
        h("div", { class: "sq-fields" },
          h("label", { class: "sq-field" }, bpmIn, h("span", { class: "k-label", text: "Tempo, BPM" })),
          h("label", { class: "sq-field" }, stepsIn, h("span", { class: "k-label", text: "Steps" })),
          gridSeg.el, staticMode ? null : clockSeg.el)),
      h("div", { class: "sq-perf", role: "group", "aria-label": "Performance" }, gateKnob.el, shuffleKnob.el, probKnob.el)),
    warn,
    well,
    h("div", { class: "sq-under" },
      h("h2", { class: "caption" }, "Piano roll", h("span", { class: "note",
        text: "Click to add a note; drag right to make it longer. Click a note to select it, double-click to delete it. On the keyboard: arrows move, Enter adds or selects, Delete removes, [ and ] change the length." })),
      inspector),
  );

  // the banks and the S-1's patterns (they need the app)
  const nameIn = h("input", { type: "text", placeholder: "Name this sequence", "aria-label": "Sequence name", maxlength: "80" });
  const saveBtn = h("button", { type: "button", class: "pill", "data-action": "save", onclick: () => save(false) }, "Save");
  const confirmBox = h("p", { class: "sq-confirm sq-hidden" });
  const list = h("ul", { class: "sq-list", "aria-label": "Saved sequences" });
  const listNote = h("p", { class: "note" });
  const bankSel = h("select", { "aria-label": "Pattern bank" }, [1, 2, 3, 4].map((b) => h("option", { value: String(b), text: String(b) })));
  const slotSel = h("select", { "aria-label": "Pattern slot" }, Array.from({ length: 16 }, (_, i) => h("option", { value: String(i + 1), text: String(i + 1) })));
  const pcBtn = h("button", { type: "button", class: "pill", "data-action": "switch-pattern", onclick: switchPattern }, "Switch pattern");
  if (staticMode) {
    view.append(h("p", { class: "note", style: "margin-top:40px" },
      "Saving sequences works when the app runs on your computer. Switching the S-1's own patterns also needs the S-1 plugged in."));
  } else {
    nameIn.addEventListener("keydown", (e) => { if (e.key === "Enter") save(false); });
    view.append(h("div", { class: "sq-lower" },
      h("section", { "aria-label": "Sequences" },
        h("h2", { class: "heading", text: "Sequences" }),
        h("p", { class: "note", text: "Save the pattern under a name, or load one you saved." }),
        h("div", { class: "sq-save" }, nameIn, saveBtn), confirmBox, list, listNote),
      h("section", { "aria-label": "Patterns on the S-1" },
        h("h2", { class: "heading", text: "Patterns on the S-1" }),
        h("p", { class: "note", text: "Switch the S-1 to one of its 64 saved patterns. This sends a program change on the pattern channel (16 unless you changed it)." }),
        h("div", { class: "sq-pc" }, h("label", {}, bankSel, "Bank"), h("label", {}, slotSel, "Slot"), pcBtn))));
  }

  function fail(e) { ctx.toast?.(e?.message ? `The app said: ${e.message}` : "The app did not answer. Check that it is still running."); }

  // ── the roll ────────────────────────────────────────────────────────────────────
  function layout() {
    const width = Math.max(200, well.clientWidth || 800);
    geo = rollGeometry({ steps: seq.steps, width });
    canvas.style.width = `${geo.w}px`;
    canvas.style.height = `${geo.h}px`;
    drawRoll();
  }
  function drawRoll({ playing, paused, position } = T.state) {
    if (!canvas.isConnected) return;
    const [c, w, hh] = draw.fit(canvas);
    const g = geo;
    // rows: white keys a shade lighter than the well (black keys stay deep)
    c.save();
    c.fillStyle = draw.FIELD;
    for (let p = g.lo; p <= g.hi; p++) {
      if (BLACK.has(p % 12)) continue;
      c.globalAlpha = 0.45;
      c.fillRect(g.labelW, rowY(g, p), w - g.labelW, g.rowH);
    }
    c.globalAlpha = 1;
    // the key strip on the left: black keys as short bars, C labels
    c.font = "400 10.5px 'Libre Franklin', sans-serif";
    c.textBaseline = "middle";
    for (let p = g.lo; p <= g.hi; p++) {
      const y = rowY(g, p);
      if (BLACK.has(p % 12)) {
        c.fillStyle = draw.FIELD; c.fillRect(0, y + 1, g.labelW * 0.52, g.rowH - 2);
        c.strokeStyle = draw.INK3; c.lineWidth = 1; c.strokeRect(0.5, y + 1.5, g.labelW * 0.52, g.rowH - 3);
      }
      if (p % 12 === 0) { c.fillStyle = draw.INK2; c.fillText(noteName(p), g.labelW * 0.56, y + g.rowH / 2 + 0.5); }
    }
    // grid: hairlines, stronger on beats and octaves
    c.lineWidth = 1;
    const beat = stepsPerBeat(seq.step_resolution);
    for (let s = 0; s <= g.steps; s++) {
      const x = stepX(g, s) + 0.5;
      c.strokeStyle = s % beat === 0 ? draw.INK3 : draw.INK4;
      c.beginPath(); c.moveTo(x, 0); c.lineTo(x, hh); c.stroke();
    }
    for (let p = g.lo; p <= g.hi + 1; p++) {
      const y = rowY(g, p - 1) + 0.5;           // the line under row p
      c.strokeStyle = p % 12 === 0 ? draw.INK3 : draw.INK4;
      c.beginPath(); c.moveTo(g.labelW, y); c.lineTo(w, y); c.stroke();
    }
    // steps with too many notes: an engraved hatch over the column
    c.strokeStyle = draw.INK3;
    for (const s of T.poly) {
      if (s >= g.steps) continue;
      const x0 = stepX(g, s);
      c.save(); c.beginPath(); c.rect(x0, 0, g.cellW, hh); c.clip();
      c.beginPath();
      for (let y = -g.cellW; y < hh + g.cellW; y += 7) { c.moveTo(x0, y + g.cellW); c.lineTo(x0 + g.cellW, y); }
      c.stroke(); c.restore();
    }
    // the playhead
    if (position >= 0 && position < g.steps && (playing || paused)) {
      c.fillStyle = draw.INK4; c.fillRect(stepX(g, position), 0, g.cellW, hh);
      const x = stepX(g, position) + 0.5;
      c.strokeStyle = draw.INK; c.lineWidth = 1.25; c.beginPath(); c.moveTo(x, 0); c.lineTo(x, hh); c.stroke();
    }
    // notes: bars in their pitch colors with an ink edge; brighter for louder
    for (const n of seq.notes) {
      if (n.pitch < g.lo || n.pitch > g.hi || n.step >= g.steps) continue;
      const r = noteRect(g, n);
      const rgb = rgbOf(n.pitch % 12);
      c.fillStyle = rgba(rgb, 0.4 + 0.6 * (n.velocity / 127));
      c.fillRect(r.x, r.y, r.w, r.h);
      c.strokeStyle = draw.INK; c.lineWidth = 1.1;
      c.strokeRect(r.x + 0.55, r.y + 0.55, r.w - 1.1, r.h - 1.1);
      if (n === selected) {                       // a dotted ring, and the length handle at the end
        c.save(); c.lineWidth = 1.25; c.setLineDash([3, 2]);
        c.strokeRect(r.x - 2.5, r.y - 2.5, r.w + 5, r.h + 5); c.restore();
        c.beginPath(); c.moveTo(r.x + r.w - 4.5, r.y + 3); c.lineTo(r.x + r.w - 4.5, r.y + r.h - 3); c.stroke();
      }
    }
    // the keyboard cursor: only for keyboard use (after an arrow key, or a keyboard focus)
    let keyFocus = false;
    try { keyFocus = canvas.matches(":focus-visible"); } catch (_) { /* older browsers */ }
    if ((cursorOn || keyFocus) && document.activeElement === canvas) {
      c.strokeStyle = draw.INK; c.lineWidth = 1.25; c.setLineDash([2, 2]);
      c.strokeRect(stepX(g, cursor.step) + 1.5, rowY(g, cursor.pitch) + 1.5, g.cellW - 3, g.rowH - 3);
      c.setLineDash([]);
    }
    c.restore();
  }
  function pointCell(e) {
    const rect = canvas.getBoundingClientRect();
    return cellAt(geo, e.clientX - rect.left, e.clientY - rect.top);
  }
  canvas.addEventListener("pointerdown", (e) => {
    const cell = pointCell(e);
    if (!cell) return;
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* a synthetic pointer cannot be captured */ }
    cursor = { ...cell };
    cursorOn = false;
    T.hold(true);                                   // the app's echoes wait until the drag ends
    const hit = noteAt(seq.notes, cell.step, cell.pitch);
    if (hit) { select(hit); drag = { note: hit }; }
    else {
      const n = { step: cell.step, pitch: cell.pitch, velocity: newVelocity, duration: 1 };
      seq.notes.push(n);
      select(n);
      drag = { note: n };
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const cell = pointCell(e);
    if (!cell) return;
    const n = drag.note, d = clampDuration(cell.step - n.step + 1, n.step, seq.steps);
    if (d !== n.duration) { n.duration = d; lenKnob.set(d); drawRoll(); }
  });
  const endDrag = () => { T.hold(false); if (drag) { drag = null; push(); } };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("dblclick", (e) => {
    const cell = pointCell(e);
    const hit = cell && noteAt(seq.notes, cell.step, cell.pitch);
    if (hit) deleteNote(hit);
  });
  canvas.addEventListener("focus", () => drawRoll());
  canvas.addEventListener("blur", () => { drawRoll(); });
  canvas.addEventListener("keydown", (e) => {
    if (e.key.startsWith("Arrow")) {
      e.preventDefault();
      cursor = moveCursor(cursor, e.key, geo, e.shiftKey);
      cursorOn = true;
      revealCursor();
      drawRoll();
    } else if (e.key === "Enter") {                 // Space is play/pause everywhere (core/shortcuts.js)
      e.preventDefault();
      cursorOn = true;
      const hit = noteAt(seq.notes, cursor.step, cursor.pitch);
      if (hit) select(hit);
      else {
        const n = { step: cursor.step, pitch: cursor.pitch, velocity: newVelocity, duration: 1 };
        seq.notes.push(n); select(n); push();
      }
    } else if ((e.key === "Delete" || e.key === "Backspace") && selected) {
      e.preventDefault(); deleteNote(selected);
    } else if ((e.key === "]" || e.key === "[") && selected) {
      e.preventDefault();
      selected.duration = clampDuration(selected.duration + (e.key === "]" ? 1 : -1), selected.step, seq.steps);
      lenKnob.set(selected.duration); drawRoll(); push();
    } else if (e.key === "Escape" && selected) {
      select(null);
    }
  });
  function revealCursor() {
    const y = rowY(geo, cursor.pitch), x = stepX(geo, cursor.step);
    if (y < well.scrollTop + 8) well.scrollTop = y - 8;
    else if (y + geo.rowH > well.scrollTop + well.clientHeight - 8) well.scrollTop = y + geo.rowH - well.clientHeight + 8;
    if (x < well.scrollLeft + geo.labelW) well.scrollLeft = x - geo.labelW;
    else if (x + geo.cellW > well.scrollLeft + well.clientWidth) well.scrollLeft = x + geo.cellW - well.clientWidth;
  }
  function select(n) {
    selected = n;
    inspector.classList.toggle("sq-hidden", !n);
    if (n) {
      whichSwatch.style.setProperty("--pc", rgbOf(n.pitch % 12).join(","));
      whichText.textContent = `${noteName(n.pitch)} at step ${n.step + 1}`;
      velKnob.set(n.velocity); lenKnob.set(n.duration);
    }
    drawRoll();
  }
  function deleteNote(n) {
    if (!n) return;
    seq.notes = seq.notes.filter((x) => x !== n);
    if (selected === n) select(null);
    drawRoll();
    push();
  }
  function refreshPoly() {
    const poly = T.poly;
    warn.textContent = warningText(poly);
    warn.classList.toggle("sq-hidden", !poly.length);
  }

  // ── the transport: edits go through it, and it tells us what changed ─────────────
  /** An edit to seq: the transport re-counts the four-note warning (static) or sends it to the app. */
  function push(now = false) { T.edited({ now }); }
  /** The transport's sequence was replaced (the app, another tab, a loaded bank): show it. The
   *  selection re-binds to the same note in the fresh list (a replacement makes new objects). */
  function adopt() {
    const keep = selected ? { step: selected.step, pitch: selected.pitch } : null;
    selected = keep ? seq.notes.find((n) => n.step === keep.step && n.pitch === keep.pitch) || null : null;
    inspector.classList.toggle("sq-hidden", !selected);
    if (selected) select(selected);
    if (document.activeElement !== bpmIn) bpmIn.value = String(seq.bpm);
    if (document.activeElement !== stepsIn) stepsIn.value = String(seq.steps);
    gridSeg.set(seq.step_resolution);
    cursor.step = Math.min(cursor.step, seq.steps - 1);
    refreshPoly();
    layout();
    if (!scrolled && seq.notes.length) {             // open on the notes, the top one with a little room
      const top = Math.max(...seq.notes.map((n) => n.pitch));
      well.scrollTop = Math.max(0, rowY(geo, Math.min(geo.hi, top + 3)) - 10);
      scrolled = true;
    }
  }
  function showPerf() {
    gateKnob.set(Math.round(perf.gate * 100)); shuffleKnob.set(Math.round(perf.shuffle * 100)); probKnob.set(Math.round(perf.probability * 100));
    clockSeg.set(perf.clock ? "on" : "off");
  }
  function paintTransport(st = T.state) {
    playBtn.setAttribute("aria-pressed", String(st.playing));
    pauseBtn.setAttribute("aria-pressed", String(st.paused));
    pos.textContent = positionText(st);
    drawRoll(st);
  }
  disposers.push(T.on((st, what) => {
    if (what === "sequence") adopt();
    else if (what === "tempo") { if (document.activeElement !== bpmIn) bpmIn.value = String(seq.bpm); }
    else if (what === "poly") refreshPoly();
    else if (what === "perf") showPerf();
    paintTransport(st);
  }));
  // Delete (core/shortcuts.js) deletes the selected note wherever focus is, except in a text field.
  const offDelete = ctx.shortcuts?.handle?.("delete", () => { if (!selected) return false; deleteNote(selected); return true; });
  if (offDelete) disposers.push(offDelete);

  bpmIn.addEventListener("change", () => { bpmIn.value = String(T.setTempo(bpmIn.value, { now: true })); });
  stepsIn.addEventListener("change", () => {
    const v = T.setSteps(stepsIn.value);                  // notes past the end go, as the app does
    stepsIn.value = String(v);
    if (selected && !seq.notes.includes(selected)) select(null);
    cursor.step = Math.min(cursor.step, v - 1);
    layout();
  });

  // ── banks and patterns ──────────────────────────────────────────────────────────
  let savedNames = [];
  async function refreshList() {
    try {
      const rows = await api("GET", "/api/sequences");
      savedNames = rows.map((r) => r.name);
      list.replaceChildren(...rows.map((r) => {
        const del = h("button", { type: "button", class: "quiet" }, "Delete");
        let armed = 0;
        del.addEventListener("click", async () => {
          if (!armed) { del.textContent = "Really delete?"; armed = later(() => { armed = 0; del.textContent = "Delete"; }, 3000); return; }
          clearTimeout(armed); armed = 0;
          try { await api("DELETE", `/api/sequences/${encodeURIComponent(r.name)}`); ctx.toast?.(`Deleted ${r.name}.`); refreshList(); }
          catch (e) { fail(e); }
        });
        return h("li", {}, h("span", { text: r.name }),
          h("button", { type: "button", class: "quiet", onclick: async () => {
            try { T.replace(await api("POST", `/api/sequences/${encodeURIComponent(r.name)}/load`)); ctx.toast?.(`Loaded ${r.name}.`); }
            catch (e) { fail(e); }
          } }, "Load"), del);
      }));
      listNote.textContent = rows.length ? "" : "No saved sequences yet.";
      listNote.classList.toggle("sq-hidden", !!rows.length);
    } catch (e) { listNote.textContent = "Could not read the saved sequences."; }
  }
  function askOverwrite(name) {
    confirmBox.replaceChildren(`A sequence named ${name} exists.`,
      h("button", { type: "button", class: "linkish", onclick: () => save(true) }, "Save over it"),
      h("button", { type: "button", class: "quiet", onclick: () => confirmBox.classList.add("sq-hidden") }, "Cancel"));
    confirmBox.classList.remove("sq-hidden");
  }
  async function save(overwrite) {
    const name = nameIn.value.trim();
    if (!name) { ctx.toast?.("Type a name first."); nameIn.focus(); return; }
    if (!validName(name)) { ctx.toast?.("A name cannot hold / or \\, or start with a dot. Choose another name."); nameIn.focus(); return; }
    // Ask before the request (a 409 from the app would still land here, below, if two tabs race).
    if (!overwrite && savedNames.some((n) => n.toLowerCase() === name.toLowerCase())) { askOverwrite(name); return; }
    try {
      await api("POST", "/api/sequences", { name, overwrite });
      ctx.toast?.(overwrite ? `Saved over ${name}.` : `Saved ${name}.`);
      nameIn.value = "";
      confirmBox.classList.add("sq-hidden");
      refreshList();
    } catch (e) {
      if (e?.status === 409 || /exists/i.test(e?.message || "")) askOverwrite(name);
      else fail(e);
    }
  }
  async function switchPattern() {
    const bank = Number(bankSel.value), slot = Number(slotSel.value);
    try {
      const r = await api("POST", "/api/device/pattern", { bank, slot });
      ctx.toast?.(ctx.status?.sync === "offline"
        ? "The S-1 is not connected, so nothing changed. Plug it in, then switch again."
        : `Switched the S-1 to pattern ${r.bank}-${r.slot}.`);
    } catch (e) { fail(e); }
  }

  // ── start ───────────────────────────────────────────────────────────────────────
  const ro = new ResizeObserver(() => layout());
  ro.observe(well);
  disposers.push(() => ro.disconnect());
  adopt();                                          // the transport already holds the sequence
  if (!scrolled) well.scrollTop = Math.max(0, rowY(geo, 79) - 10);   // an empty roll opens around C4–G5
  if (!staticMode) {
    T.refresh();                                    // the performance settings may have changed in another tab
    refreshList();
  }
  paintTransport();

  return {
    destroy() {
      for (const t of timers) clearTimeout(t);
      timers.clear();
      for (const d of disposers.reverse()) { try { d(); } catch (_) { /* keep tearing down */ } }
    },
  };
}
