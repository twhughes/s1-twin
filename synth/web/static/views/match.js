// views/match.js — the Match view: drop a sound, watch gradient descent find the twin's knobs.
// Contract: docs/design/BUILD.md §2.2 (ctx), §2.4 (/ws/match frames, recorded matches).
// Server mode streams a live match from /ws/match; static mode (ctx.server === null) replays
// recorded runs from matches/index.json with the same visuals. The pure helpers below
// (reduceFrame, phaseText, replaySchedule, lossDomain, …) run in node: views/match.check.mjs.

import { knob } from "../design/knob.js";
import { seg, GLYPHS } from "../design/seg.js";
import * as draw from "../design/draw.js";
import { rgbOf, lum, noteName } from "../design/colors.js";

export const id = "match";
export const title = "Match";

// ── what the twin models: 18 knobs + 3 switches, in the plate's signal order ─────────
export const STAGES = [
  { name: "Oscillator", controls: [
    { cc: 20, label: "Saw" }, { cc: 19, label: "Square" }, { cc: 21, label: "Sub" }, { cc: 23, label: "Noise" },
    { cc: 15, label: "Pulse width" }, { cc: 13, label: "Vibrato" }, { cc: 76, label: "Fine tune", bipolar: true },
    { cc: 22, label: "Sub octave", options: [[2, "−1"], [1, "−2"], [0, "−2 asym"]] },
  ] },
  { name: "Filter", controls: [
    { cc: 74, label: "Cutoff" }, { cc: 71, label: "Resonance" }, { cc: 24, label: "Env amount" },
    { cc: 25, label: "LFO amount" }, { cc: 26, label: "Key follow" },
  ] },
  { name: "Envelope", controls: [
    { cc: 73, label: "Attack" }, { cc: 75, label: "Decay" }, { cc: 30, label: "Sustain" }, { cc: 72, label: "Release" },
    { cc: 28, label: "Volume shape", options: [[0, "Gate"], [1, "Envelope"]] },
  ] },
  { name: "LFO", controls: [
    { cc: 3, label: "Rate" }, { cc: 17, label: "Mod wheel to LFO" },
    { cc: 12, label: "Wave", glyphs: [[0, "saw", "Saw"], [1, "isaw", "Inverse saw"], [2, "tri", "Triangle"],
      [3, "sq", "Square"], [4, "rnd", "Random"], [5, "noise", "Noise"]] },
  ] },
];
export const TWIN_CCS = STAGES.flatMap((s) => s.controls.map((c) => c.cc));
// s1.json defaults, used only when ctx.params lacks a value.
const DEFAULTS = { 20: 0, 19: 127, 21: 0, 23: 0, 15: 0, 13: 0, 76: 64, 22: 2, 74: 127, 71: 0, 24: 0, 25: 0,
  26: 0, 73: 0, 75: 42, 30: 25, 72: 21, 28: 1, 3: 60, 17: 15, 12: 2 };

// ── pure: the run state, folded frame by frame ─────────────────────────────────────
export function initialRun() {
  return { phase: "idle", count: 0, points: [], marks: [], notes: [], seeded: null, chord: "",
    cc: null, bestCC: null, bestWave: null, wave: null, targetWave: null, iter: 0, total: 0, restart: 0,
    nsTotal: 0, improved: false, done: null, error: null };
}

const num = (x) => (typeof x === "number" && Number.isFinite(x) ? x : NaN);
export const prettyChord = (s) => String(s || "").replaceAll("#", "♯").replaceAll("+", " + ");

/** Fold one /ws/match frame into the run state (returns a new state; never mutates). */
export function reduceFrame(s, f) {
  if (!f || typeof f !== "object") return s;
  if (f.phase === "error") return { ...s, phase: "error", error: String(f.detail || "The matcher stopped.") };
  const next = { ...s, count: s.count + 1, phase: f.phase };
  if (Array.isArray(f.notes)) { next.notes = f.notes.slice(); next.chord = prettyChord(f.chord_name); }
  if (typeof f.seeded === "boolean") next.seeded = f.seeded;
  if (f.cc) next.cc = f.cc;
  if (f.wave) next.wave = f.wave;
  if (f.target_wave) next.targetWave = f.target_wave;
  if (f.phase === "done") {
    next.done = { closeness: num(f.closeness), seconds: num(f.seconds), steps: f.steps | 0, cc: f.cc || s.bestCC,
      notes: next.notes, matchWav: f.match_wav_b64 || null, targetWav: f.target_wav_b64 || null };
    next.bestCC = f.cc || s.bestCC;
    return next;
  }
  const prev = s.points[s.points.length - 1];
  const point = { loss: num(f.loss), best: num(f.best_loss), phase: f.phase, restart: f.restart ?? (prev ? prev.restart : 0) };
  next.points = [...s.points, point];
  // Leaders on the curve: where a new start begins, and where the note search begins.
  if (prev && point.phase === "note-search" && prev.phase !== "note-search") {
    next.marks = [...s.marks, { i: next.points.length - 1, label: "Nearby notes" }];
  } else if (prev && point.phase === "gd" && point.restart > 0 && prev.restart !== point.restart) {
    next.marks = [...s.marks, { i: next.points.length - 1, label: `Start ${point.restart + 1}` }];
  }
  // The frame that sets (or ties) the best loss carries the best candidate so far.
  if (f.cc && Number.isFinite(point.loss) && point.loss === point.best) { next.bestCC = f.cc; next.bestWave = f.wave || s.bestWave; }
  if (f.phase === "note-search") { next.nsTotal = f.total | 0; next.improved = !!f.improved; next.iter = f.iter | 0; }
  else { next.iter = f.iter | 0; next.total = f.total | 0; next.restart = f.restart | 0; }
  return next;
}

/** The phase in plain words, plus a detail line. */
export function phaseText(s, { staticMode = false, loaded = false } = {}) {
  switch (s.phase) {
    case "idle":
      if (staticMode) return { word: "Choose a recorded run", detail: "" };
      return loaded ? { word: "Ready to match", detail: "Mark the notes you hear, then press Match." }
        : { word: "Waiting for a sound", detail: "" };
    case "connecting": return { word: "Opening the matcher", detail: "" };
    case "pitch": return { word: "Finding the notes", detail: (s.seeded ? "Marked: " : "Found: ") + s.chord };
    case "gd": {
      const last = s.total && s.iter >= s.total && s.seeded;
      return { word: "Descending", detail: `Step ${s.iter} of ${s.total}` + (s.restart ? ` · start ${s.restart + 1}` : "")
        + (last ? " · choosing the switches" : "") };
    }
    case "note-search": {
      const last = s.nsTotal && s.iter >= s.nsTotal;
      return { word: "Trying nearby notes", detail: s.chord + (s.improved ? " · closer" : "")
        + (last ? " · choosing the switches" : "") };
    }
    case "done": return { word: "Done", detail: (s.chord ? `${s.chord} · ` : "")
      + (s.done ? `${fmtSeconds(s.done.seconds)} · ${s.done.steps} steps` : "") };
    case "stopped": return { word: "Stopped", detail: s.bestCC ? "The best patch so far is on the knobs." : "" };
    case "error": return { word: "Could not match", detail: s.error || "" };
    default: return { word: "", detail: "" };
  }
}
export function fmtSeconds(sec) {
  if (!Number.isFinite(sec)) return "";
  if (sec < 60) return `${sec.toFixed(1)} s`;
  const m = Math.floor(sec / 60), r = Math.round(sec - m * 60);
  return `${m} min ${r} s`;
}

/** Log-scale y domain covering every loss and best-so-far point (padded). */
export function lossDomain(points) {
  let lo = Infinity, hi = -Infinity;
  for (const p of points) for (const v of [p.loss, p.best]) if (v > 0 && Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (!(hi >= lo)) return { lo: 0.1, hi: 1 };
  if (hi / lo < 1.2) { const m = Math.sqrt(hi * lo); lo = m / 1.1; hi = m * 1.1; }
  const pad = Math.pow(hi / lo, 0.06);
  return { lo: lo / pad, hi: hi * pad };
}

/** How many steps the curve's x axis should hold: what has arrived, or what the run announced. */
export function expectedSteps(s) {
  return Math.max(s.points.length, 1 + (s.total || 0) + (s.nsTotal || 0), 2);
}

/** Replay timing for a recorded run: ms offset of each frame. Even spacing within
 *  [minGap, maxGap] aiming at `total`, plus a pause at each phase or restart change. */
export function replaySchedule(frames, { total = 12000, minGap = 12, maxGap = 70, pause = 450 } = {}) {
  const n = frames.length;
  if (!n) return [];
  const gap = Math.max(minGap, Math.min(maxGap, total / Math.max(1, n - 1)));
  const out = [0];
  for (let i = 1; i < n; i++) {
    const a = frames[i - 1], b = frames[i];
    let t = out[i - 1] + gap;
    if (a.phase !== b.phase || (b.phase === "gd" && (a.restart ?? 0) !== (b.restart ?? 0))) t += pause;
    out.push(t);
  }
  return out;
}

/** The loud/quiet relation of target and candidate at the plume moment, as two loop scales. */
export function plumeLevels(target, cand) {
  const t = target ? Math.max(0, target.level || 0) : 0, c = cand ? Math.max(0, cand.level || 0) : 0;
  const top = Math.max(t, c, 1e-6), f = (v) => Math.max(0.12, Math.min(1, v / top));
  return { target: target ? f(t) : 0, cand: cand ? f(c) : 0 };
}

/** The recorded-runs index: a list of rows (or {matches: [...]}) → [{slug, title, …}]. */
export function indexRows(data) {
  const rows = Array.isArray(data) ? data : Array.isArray(data?.matches) ? data.matches : [];
  return rows.filter((r) => r && typeof r.slug === "string" && /^[a-z0-9-]+$/.test(r.slug))
    .map((r) => ({ slug: r.slug, title: String(r.title || r.slug), notes: Array.isArray(r.notes) ? r.notes : [],
      recorded: r.recorded || "", closeness: Number.isFinite(r.closeness) ? r.closeness : null }));
}

/** The ?init= CC map from the synth's current knobs (only the CCs the twin models). */
export function initMap(params) {
  const out = {};
  for (const cc of TWIN_CCS) {
    const v = params && typeof params.get === "function" ? params.get(cc) : params?.[cc];
    if (Number.isFinite(v)) out[cc] = v;
  }
  return out;
}

// ── the view ─────────────────────────────────────────────────────────────────────────
const CSS = `
.v-match { position: relative; z-index: 1; max-width: 1480px; margin: 0 auto; padding: 30px var(--gutter) 56px; }
.v-match .mx-grid { display: grid; grid-template-columns: minmax(0, 340px) minmax(0, 1fr); gap: var(--gap); align-items: start; }
.v-match .mx-input > * + * { margin-top: 18px; }
.v-match .mx-input .heading { margin-bottom: 0; }
.v-match .mx-input .note { margin: 6px 0 0; max-width: 40ch; }
.v-match .mx-drop { position: relative; min-height: 150px; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; text-align: center; padding: 18px; cursor: pointer; outline: 1.25px dashed var(--ink-3); outline-offset: -1px; }
.v-match .mx-drop:hover, .v-match .mx-drop.over { outline-color: var(--ink); }
.v-match .mx-drop:focus-visible { outline: 1.5px dashed var(--ink); outline-offset: 3px; }
.v-match .mx-drop canvas { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.v-match .mx-drop p { position: relative; margin: 0; font-size: 14px; color: var(--ink); max-width: 30ch; }
.v-match .mx-drop .mx-file { font-size: 12.5px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.v-match .mx-drop.loaded { justify-content: flex-end; }
.v-match .mx-drop.loaded p.mx-ask { display: none; }
.v-match .mx-sub { font: italic 400 19px/1.2 var(--serif); margin: 0; }
.v-match .mx-input > .mx-sub { margin-top: 26px; }
.v-match .mx-keys { position: relative; height: 70px; display: flex; background: var(--deep); border-radius: 3px; user-select: none; -webkit-user-select: none; }
.v-match .mx-keys button { font: inherit; padding: 0; margin: 0; }
.v-match .mx-wk { position: relative; flex: 1; border: 0; border-right: 1px solid var(--ink-4); background: none; cursor: pointer;
  display: flex; align-items: flex-end; justify-content: center; padding-bottom: 6px !important; font-size: 10.5px; color: var(--ink-3); border-radius: 0; }
.v-match .mx-wk.last { border-right: 0; }
.v-match .mx-bk { position: absolute; top: 0; height: 42px; background: var(--field); border: 1px solid var(--ink-3); border-top: 0;
  border-radius: 0 0 2px 2px; cursor: pointer; z-index: 2; color: var(--ink-3); font-size: 10px; }
.v-match .mx-keys [aria-pressed="true"] { background: rgb(var(--pc)); color: var(--on-pc); box-shadow: inset 0 0 0 1.5px var(--ink); }
.v-match .mx-keys.found [aria-pressed="true"] { box-shadow: inset 0 0 0 1.5px var(--ink), inset 0 0 0 3px var(--deep); }
.v-match .mx-keys button:focus-visible { outline-offset: -3px; }
.v-match .mx-keys[aria-disabled="true"] button { cursor: default; }
.v-match .mx-keyrow { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-top: 8px; }
.v-match .mx-keyrow .mx-marked { color: var(--ink-2); font-size: 13px; margin-right: auto; font-variant-numeric: tabular-nums; }
.v-match .mx-go { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.v-match .mx-run { min-width: 0; }
.v-match .mx-wells { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 24px; }
.v-match .mx-wells figure { margin: 0; min-width: 0; }
.v-match .mx-wells canvas { height: 214px; }
.v-match .mx-status { margin: 26px 0 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; min-height: 30px; }
.v-match .mx-word { font: italic 400 26px/1.1 var(--serif); }
.v-match .mx-detail { color: var(--ink-2); font-size: 13.5px; font-variant-numeric: tabular-nums; }
.v-match .mx-tag { flex-basis: 100%; font-size: 12.5px; color: var(--ink-2); }
.v-match .mx-tag b { font-weight: 500; color: var(--ink); border: 1.25px solid var(--ink-3); border-radius: 999px; padding: 2px 9px; margin-right: 8px; }
.v-match .mx-result { margin-top: 18px; display: flex; flex-wrap: wrap; align-items: center; gap: 14px 28px; }
.v-match .mx-close { display: flex; align-items: baseline; gap: 10px; }
.v-match .mx-close b { font: 300 44px/1 var(--sans); font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.v-match .mx-close span { font-size: 13.5px; color: var(--ink-2); }
.v-match .mx-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 12px; }
.v-match .mx-result .note { flex-basis: 100%; margin: 0; max-width: 62ch; }
.v-match .mx-knobs { margin-top: 34px; display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 30px var(--gap); }
.v-match .mx-stage .row { gap: 16px 10px; }
.v-match .mx-stage h3 { font: italic 400 22px/1.15 var(--serif); margin: 0 0 14px; }
.v-match .mx-knobs svg, .v-match .mx-knobs .seg-opts button { pointer-events: none; cursor: default; }
.v-match .mx-honest { margin-top: 34px !important; max-width: 70ch !important; }
.v-match .mx-recorded { list-style: none; margin: 0; padding: 0; }
.v-match .mx-recorded button { background: none; border: 0; border-bottom: 1px solid var(--ink-4); cursor: pointer; width: 100%;
  text-align: left; padding: 9px 0; color: var(--ink-2); font-size: 14.5px; display: flex; justify-content: space-between; gap: 12px; }
.v-match .mx-recorded button:hover, .v-match .mx-recorded button[aria-current="true"] { color: var(--ink); }
.v-match .mx-recorded small { font-size: 12.5px; color: var(--ink-2); font-variant-numeric: tabular-nums; white-space: nowrap; }
.v-match code { font: 400 12.5px ui-monospace, "SF Mono", Menlo, monospace; color: var(--ink); background: var(--deep); padding: 1px 5px; border-radius: 3px; white-space: nowrap; }
.v-match .mx-hidden { display: none !important; }
@media (max-width: 1100px) {
  .v-match .mx-grid { grid-template-columns: minmax(0, 1fr); }
  .v-match .mx-input .note { max-width: 60ch; }
}
@media (max-width: 640px) {
  .v-match .mx-wells { grid-template-columns: minmax(0, 1fr); }
  .v-match .mx-wells canvas { height: 180px; }
  .v-match .mx-close b { font-size: 36px; }
}
`;

let current = null;   // the mounted instance (one at a time)

export function mount(root, ctx) {
  if (current) unmount();
  current = createView(root, ctx);
}

export function unmount() {
  if (!current) return;
  try { current.destroy(); } finally { current = null; }
}

export default { id, title, mount, unmount };

// A tiny element builder: h("div", {class: "x", onclick: fn}, child, "text", …)
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
  const disposers = [];
  const style = h("style", { "data-view": "match" });
  style.textContent = CSS;
  document.head.append(style);
  disposers.push(() => style.remove());

  let run = initialRun();
  let running = false;          // a live match or a replay is streaming
  let recorded = false;         // the frames on screen come from a recorded run
  let socket = null;
  let replayTimers = [];
  let file = null, fileBytes = null, targetBuffer = null;
  let quality = "quick", startFrom = "scratch";
  const seeds = new Set();
  let lowC = 48;                // the keyboard shows two octaves from here
  let showRunNotes = true;      // after a run the keyboard shows its notes, until a key is pressed
  let audio = null, source = null;
  let raf = 0;

  // ── layout ──────────────────────────────────────────────────────────────────────
  const view = h("section", { class: "v-match", "aria-label": "Match a sound", "data-phase": "idle" });
  const input = h("div", { class: "mx-input" });
  const runCol = h("div", { class: "mx-run" });
  view.append(h("div", { class: "mx-grid" }, input, runCol));
  root.append(view);
  disposers.push(() => view.remove());

  input.append(
    h("h2", { class: "heading", text: "Match a sound" }),
    h("p", { class: "note", text: staticMode
      ? "The matcher turns the twin's knobs by gradient descent until the twin sounds like a recording. This page cannot run it, so here are real runs, recorded on a computer and replayed step by step."
      : "Give it a recording of one note or a chord of up to four. The matcher turns the twin's knobs by gradient descent until the twin sounds like it." }),
  );

  // server mode: the drop well, the notes, the search budget
  const fileInput = h("input", { type: "file", accept: ".wav,.wave,.aif,.aiff,.flac,.ogg,audio/*", hidden: true, "aria-hidden": "true", tabindex: "-1" });
  const dropCanvas = h("canvas", { "aria-hidden": "true" });
  const dropAsk = h("p", { class: "mx-ask" }, "Drop a sound here, or ",
    h("button", { type: "button", class: "linkish", "data-action": "choose", onclick: (e) => { e.stopPropagation(); if (!running) fileInput.click(); } }, "choose a file"));
  const dropFile = h("p", { class: "mx-file" });
  const drop = h("div", { class: "well mx-drop", role: "button", tabindex: "0", "data-role": "drop",
    "aria-label": "Drop a sound here, or press Enter to choose a file" }, dropCanvas, dropAsk, dropFile, fileInput);
  const playTargetEarly = h("button", { type: "button", class: "quiet", disabled: true, "data-action": "play-target-early",
    onclick: () => playBuffer("target") }, "Play target");
  const earlyRow = h("div", { class: "mx-go mx-hidden" }, playTargetEarly);

  const keys = h("div", { class: "mx-keys", role: "group", "aria-label": "Notes to match" });
  const marked = h("span", { class: "mx-marked", "aria-live": "polite" });
  const qualitySeg = seg({ label: "Search", value: quality, options: [{ value: "quick", label: "Quick" }, { value: "thorough", label: "Thorough" }],
    onInput: (v) => { quality = v; } });
  const startSeg = seg({ label: "Start from", value: startFrom, options: [{ value: "scratch", label: "Scratch" }, { value: "current", label: "The synth's knobs" }],
    onInput: (v) => { startFrom = v; } });
  const matchBtn = h("button", { type: "button", class: "pill", disabled: true, "data-action": "match", onclick: () => (running ? stopRun() : startMatch()) }, "Match");
  const goNote = h("span", { class: "note", style: "margin:0" });

  if (!staticMode) {
    input.append(
      drop,
      earlyRow,
      h("div", {},
        h("h3", { class: "mx-sub", text: "Which notes?" }),
        h("p", { class: "note", text: "Mark the notes you hear, up to four. This is the sure way. Leave them empty and the matcher looks for them." }),
        h("div", { style: "margin-top:12px" }, keys),
        h("div", { class: "mx-keyrow" }, marked,
          h("button", { type: "button", class: "quiet", onclick: () => shiftKeys(-12) }, "Lower"),
          h("button", { type: "button", class: "quiet", onclick: () => shiftKeys(12) }, "Higher"),
          h("button", { type: "button", class: "quiet", onclick: () => { seeds.clear(); paintKeys(); } }, "Clear")),
      ),
      h("div", { class: "row", style: "gap:14px 34px" }, qualitySeg.el, startSeg.el),
      h("p", { class: "note", text: "Quick is one short descent. Thorough makes four starts and keeps the best; it takes several times longer." }),
      h("div", { class: "mx-go" }, matchBtn, goNote),
    );
  }

  // static mode: the recorded runs
  const recList = h("ul", { class: "mx-recorded", "aria-label": "Recorded runs" });
  const recNote = h("p", { class: "note" });
  if (staticMode) {
    input.append(
      h("h3", { class: "mx-sub", text: "Recorded runs" }),
      recList, recNote,
      h("p", { class: "note" }, "To match your own sounds, run the app on your computer. In its repository folder, run ",
        h("code", { text: 'pip install -e ".[studio,twin]"' }), ", then ", h("code", { text: "synth" }), "."),
    );
  }

  // the run: two wells, the status line, the result, the knobs
  const lossCanvas = h("canvas", { class: "well", role: "img", "aria-label": "The loss, falling as the matcher descends" });
  const plumeCanvas = h("canvas", { class: "well", role: "img", "aria-label": "The target and the current guess, drawn as plumes" });
  const word = h("span", { class: "mx-word", "aria-live": "polite" });
  const detail = h("span", { class: "mx-detail" });
  const tag = h("span", { class: "mx-tag mx-hidden", "data-role": "recorded" });
  const closeNum = h("b");
  const closeNote = h("p", { class: "note", text: "Closeness is the app's own measure of how alike the two sound. No one has checked it by ear yet, so trust your ears first." });
  const playTarget = h("button", { type: "button", class: "pill", "data-action": "play-target", onclick: () => playBuffer("target") }, "Play target");
  const playMatch = h("button", { type: "button", class: "pill", "data-action": "play-match", onclick: () => playBuffer("match") }, "Play match");
  const loadBtn = h("button", { type: "button", class: "pill", "data-action": "load", onclick: loadIntoSynth }, "Load into the synth");
  const againBtn = h("button", { type: "button", class: "quiet", "data-action": "again", onclick: () => (recorded ? replayAgain() : startMatch()) }, "Match again");
  const closeBlock = h("div", { class: "mx-close" }, closeNum, h("span", { text: "closeness" }));
  const result = h("div", { class: "mx-result mx-hidden", "data-role": "result" },
    closeBlock, h("div", { class: "mx-actions" }, playTarget, playMatch, loadBtn, againBtn), closeNote);
  const knobsBox = h("div", { class: "mx-knobs", "aria-label": "The twin's knobs, as the matcher sets them" });
  runCol.append(
    h("div", { class: "mx-wells" },
      h("figure", {}, lossCanvas, h("figcaption", { class: "caption" }, "Descent",
        h("span", { class: "note", text: "The loss, lower is closer. The bright line is the best so far." }))),
      h("figure", {}, plumeCanvas, h("figcaption", { class: "caption" }, "Target and guess",
        h("span", { class: "note", text: "Solid: the target. Dotted: the twin's current guess. Each loop is one cycle." })))),
    h("p", { class: "mx-status", role: "status" }, tag, word, detail),
    result,
    knobsBox,
    h("p", { class: "note mx-honest", text: "The matcher finds a patch that sounds like the target, not always the patch that made it. The twin's curves are not yet calibrated to a real S-1, so a match is only as true as the twin." }),
  );

  // the knobs and switches (read-only here: they show the matcher's hand)
  const controls = new Map();   // cc -> {set(v)}
  for (const stage of STAGES) {
    const row = h("div", { class: "row" });
    for (const c of stage.controls) {
      const v0 = startValue(c.cc);
      let ctl;
      if (c.options || c.glyphs) {
        const options = c.glyphs
          ? c.glyphs.map(([value, g, label]) => ({ value, label, glyph: GLYPHS[g] }))
          : c.options.map(([value, label]) => ({ value, label }));
        ctl = seg({ label: c.label, value: v0, options });
        ctl.el.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        ctl.el.setAttribute("aria-readonly", "true");
      } else {
        ctl = knob({ label: c.label, min: 0, max: 127, value: v0, def: DEFAULTS[c.cc], bipolar: !!c.bipolar });
        const svg = ctl.el.querySelector("svg");
        svg.setAttribute("tabindex", "-1");
        svg.setAttribute("aria-readonly", "true");
      }
      controls.set(c.cc, ctl);
      row.append(ctl.el);
    }
    knobsBox.append(h("div", { class: "mx-stage" }, h("h3", { text: stage.name }), row));
  }

  function startValue(cc) {
    const v = ctx.params && typeof ctx.params.get === "function" ? ctx.params.get(cc) : undefined;
    return Number.isFinite(v) ? v : DEFAULTS[cc];
  }
  function showCC(cc) {
    if (!cc) return;
    for (const [key, value] of Object.entries(cc)) {
      const ctl = controls.get(Number(key));
      if (ctl && Number.isFinite(value)) ctl.set(value);
    }
  }
  // While idle, the knobs mirror the synth (e.g. the S-1's own knob twists).
  const onParam = ({ cc, value }) => { if (run.phase === "idle" && controls.has(cc)) controls.get(cc).set(value); };
  const off = typeof ctx.on === "function" ? ctx.on("param", onParam) : null;
  disposers.push(() => { if (typeof off === "function") off(); else ctx.off?.("param", onParam); });

  // ── the seeding keyboard ────────────────────────────────────────────────────────
  function buildKeys() {
    keys.replaceChildren();
    const whites = [0, 2, 4, 5, 7, 9, 11], blacks = { 1: 0, 3: 1, 6: 3, 8: 4, 10: 5 };
    const nWhite = 14, bw = 100 / nWhite;
    for (let o = 0; o < 2; o++) {
      for (const pc of whites) {
        const n = lowC + o * 12 + pc;
        keys.append(keyButton(n, "mx-wk" + (o === 1 && pc === 11 ? " last" : ""), pc === 0 ? noteName(n) : ""));
      }
    }
    for (let o = 0; o < 2; o++) {
      for (const [pc, wi] of Object.entries(blacks)) {
        const n = lowC + o * 12 + Number(pc);
        const b = keyButton(n, "mx-bk", "");
        const left = (o * 7 + wi + 1) * bw - bw * 0.3;
        b.style.left = `${left}%`; b.style.width = `${bw * 0.6}%`;
        keys.append(b);
      }
    }
    paintKeys();
  }
  function keyButton(n, cls, label) {
    const rgb = rgbOf(n % 12);
    const b = h("button", { type: "button", class: cls, "data-note": String(n), "aria-label": noteName(n), "aria-pressed": "false",
      onclick: () => toggleSeed(n) }, label);
    b.style.setProperty("--pc", rgb.join(","));
    b.style.setProperty("--on-pc", lum(rgb) > 0.5 ? "var(--field)" : "var(--ink)");
    return b;
  }
  const runNotesShown = () => showRunNotes && run.phase !== "idle" && run.notes.length > 0;
  function paintKeys() {
    const fromRun = runNotesShown();
    const on = fromRun ? new Set(run.notes) : seeds;
    keys.classList.toggle("found", fromRun && run.seeded === false);
    keys.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(on.has(Number(b.dataset.note)))));
    keys.setAttribute("aria-disabled", String(running));
    const list = [...on].sort((a, b) => a - b).map(noteName).join(" + ");
    if (fromRun) marked.textContent = (run.seeded ? "Marked: " : "Found: ") + list;
    else marked.textContent = seeds.size ? `Marked: ${list}` : "No notes marked";
  }
  function shiftKeys(d) {
    lowC = Math.max(0, Math.min(96, lowC + d));
    buildKeys();
  }
  function toggleSeed(n) {
    if (running) return;
    showRunNotes = false;         // the keys now edit the marks for the next run
    if (seeds.has(n)) seeds.delete(n);
    else if (seeds.size >= 4) { ctx.toast?.("Mark four notes at most."); return; }
    else seeds.add(n);
    audition(n);
    paintKeys();
  }
  // A short audition of a marked key. Its note-off is never cancelled (a stuck note on the
  // S-1 is worse than a late one); unmount sends any pending note-offs at once.
  const auditions = new Map();   // note -> timer
  function noteOff(n) { auditions.delete(n); try { ctx.note?.(n, false); } catch (_) { /* courtesy */ } }
  function audition(n) {
    try {
      if (auditions.has(n)) { clearTimeout(auditions.get(n)); noteOff(n); }
      ctx.note?.(n, true, 90);
      auditions.set(n, setTimeout(() => noteOff(n), 260));
    } catch (_) { /* sound is a courtesy here */ }
  }

  // ── the target file ─────────────────────────────────────────────────────────────
  drop.addEventListener("click", () => { if (!running) fileInput.click(); });
  drop.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && !running) { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer?.files?.[0]; if (f && !running) loadFile(f); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) loadFile(fileInput.files[0]); fileInput.value = ""; });

  async function loadFile(f) {
    if (running) return;                 // a run in progress keeps its target
    if (f.size > 25 * 1024 * 1024) { setError("That file is too large (25 MB max). Trim it to a few seconds of the sound."); return; }
    file = f;
    fileBytes = await f.arrayBuffer();
    targetBuffer = null;
    run = initialRun(); recorded = false;
    drop.classList.add("loaded");
    dropFile.textContent = f.name;
    matchBtn.disabled = false;
    goNote.textContent = "";
    renderAll();
    try {
      targetBuffer = await ac().decodeAudioData(fileBytes.slice(0));
      dropFile.textContent = `${f.name} · ${targetBuffer.duration.toFixed(1)} s`;
      playTargetEarly.disabled = false;
      earlyRow.classList.remove("mx-hidden");
    } catch (_) {
      playTargetEarly.disabled = true;   // e.g. AIFF: the browser cannot decode it; the server still can
      earlyRow.classList.add("mx-hidden");
    }
    drawDrop();
  }
  function drawDrop() {
    if (!dropCanvas.isConnected) return;
    const [c, w, hh] = draw.fit(dropCanvas);
    if (!targetBuffer) return;
    const data = targetBuffer.getChannelData(0);
    const top = draw.peaksPerColumn(data, Math.max(2, Math.floor(w / 2)));
    c.save(); c.translate(0, -12); draw.hatchShape(c, w, hh, top, { step: 2 }); c.restore();
  }

  // ── sound (A/B) ─────────────────────────────────────────────────────────────────
  function ac() {
    if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
    return audio;
  }
  function stopSound() { if (source) { try { source.stop(); } catch (_) { /* already stopped */ } source = null; } }
  const decoded = { target: null, match: null };
  async function wavBuffer(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return ac().decodeAudioData(bytes.buffer);
  }
  async function playBuffer(which) {
    try {
      const A = ac();
      if (A.state === "suspended") await A.resume();
      let buf = decoded[which];
      if (!buf && which === "target" && targetBuffer && !run.done) buf = targetBuffer;
      if (!buf && run.done) {
        const b64 = which === "target" ? run.done.targetWav : run.done.matchWav;
        if (b64) buf = decoded[which] = await wavBuffer(b64);
      }
      if (!buf) return;
      stopSound();
      source = A.createBufferSource();
      source.buffer = buf;
      const g = A.createGain(); g.gain.value = 0.9;
      source.connect(g); g.connect(A.destination);
      source.start();
    } catch (e) {
      ctx.toast?.("Could not play that sound in this browser.");
    }
  }

  // ── a live match ────────────────────────────────────────────────────────────────
  function startMatch() {
    if (running || !fileBytes || staticMode) return;
    stopSound();
    decoded.target = decoded.match = null;
    run = { ...initialRun(), phase: "connecting" };
    recorded = false;
    showRunNotes = true;
    setRunning(true);
    renderAll();
    revealRun();
    const q = new URLSearchParams({ throttle: "0.02", quality });
    if (seeds.size) q.set("notes", [...seeds].sort((a, b) => a - b).join(","));
    if (startFrom === "current") q.set("init", JSON.stringify(initMap(ctx.params)));
    let ws;
    try { ws = ctx.server.ws(`/ws/match?${q}`); } catch (e) { setError("Could not open the matcher. Check that the app is still running, then try again."); return; }
    socket = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { try { ws.send(fileBytes); } catch (_) { setError("Could not send the sound to the matcher. Try again."); } };
    ws.onmessage = (ev) => {
      let f; try { f = JSON.parse(ev.data); } catch (_) { return; }
      apply(f);
      if (f.phase === "done" || f.phase === "error") { socket = null; setRunning(false); try { ws.close(); } catch (_) { /* closed */ } }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      if (run.phase !== "done" && run.phase !== "error" && run.phase !== "stopped") {
        setError("The connection to the app closed before the match finished. Check that the app is still running, then try again.");
      }
    };
  }
  function stopRun() {
    if (socket) { const ws = socket; socket = null; try { ws.close(); } catch (_) { /* closed */ } }
    replayTimers.forEach(clearTimeout); replayTimers = [];
    if (running) run = { ...run, phase: "stopped" };
    setRunning(false);
    renderAll();
  }
  function setError(msg) {
    run = { ...run, phase: "error", error: msg };
    setRunning(false);
    renderAll();
  }
  function setRunning(on) {
    running = on;
    view.dataset.phase = run.phase;
    matchBtn.textContent = on ? "Stop" : "Match";
    matchBtn.disabled = !on && !fileBytes;
    drop.setAttribute("aria-disabled", String(on));
    paintKeys();
  }

  // One frame from the socket or a replay: fold it in, move the knobs, redraw.
  function apply(f) {
    run = reduceFrame(run, f);
    view.dataset.phase = run.phase;
    schedule();
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; renderAll(); }); }
  // On a narrow screen the wells sit below the inputs: bring them into view when a run starts.
  function revealRun() {
    const top = runCol.getBoundingClientRect().top;
    if (top > window.innerHeight * 0.6) {
      const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      runCol.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
    }
  }

  // ── recorded runs (static mode) ─────────────────────────────────────────────────
  let recordedRows = [], currentRec = null;
  const matchesURL = (p) => new URL(`../matches/${p}`, import.meta.url);
  async function loadIndex() {
    recNote.textContent = "Looking for recorded runs…";
    try {
      const r = await fetch(matchesURL("index.json"), { cache: "no-cache" });
      if (!r.ok) throw new Error(String(r.status));
      recordedRows = indexRows(await r.json());
    } catch (_) {
      recordedRows = [];
    }
    recList.replaceChildren(...recordedRows.map((row) => h("li", {},
      h("button", { type: "button", "data-slug": row.slug, onclick: () => playRecorded(row) },
        h("span", { text: row.title }),
        h("small", { text: [row.notes.map(noteName).join(" + "), row.closeness != null ? `${Math.round(row.closeness)}%` : ""].filter(Boolean).join(" · ") })))));
    recNote.textContent = recordedRows.length ? "Choose one to watch the matcher work." : "No recorded runs are published here yet.";
  }
  async function playRecorded(row) {
    stopRun();
    recList.querySelectorAll("button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.slug === row.slug)));
    let data;
    try {
      const r = await fetch(matchesURL(`${row.slug}.json`), { cache: "no-cache" });
      if (!r.ok) throw new Error(String(r.status));
      data = await r.json();
    } catch (_) {
      setError("Could not load that recorded run. Reload the page and try again.");
      return;
    }
    currentRec = data;
    replay(data);
  }
  function replay(data) {
    const frames = Array.isArray(data?.frames) ? data.frames : [];
    if (!frames.length) { setError("That recorded run has no frames."); return; }
    stopSound();
    decoded.target = decoded.match = null;
    run = initialRun();
    recorded = true;
    showRunNotes = true;
    setRunning(true);
    renderAll();
    revealRun();
    const times = replaySchedule(frames);
    replayTimers = frames.map((f, i) => setTimeout(() => {
      apply(f);
      if (i === frames.length - 1) setRunning(false);
    }, times[i]));
  }
  function replayAgain() { if (currentRec) replay(currentRec); }

  // ── the result ──────────────────────────────────────────────────────────────────
  function loadIntoSynth() {
    const cc = run.done?.cc || run.bestCC;
    if (!cc) return;
    let n = 0;
    for (const [key, value] of Object.entries(cc)) {
      if (!Number.isFinite(value)) continue;
      ctx.set(Number(key), value, { source: "match" });
      n++;
    }
    const s1 = ctx.soundSource === "s1";
    ctx.toast?.(s1 ? `Loaded the match into the synth and the S-1 (${n} settings).` : `Loaded the match into the synth (${n} settings).`);
  }

  // ── drawing ─────────────────────────────────────────────────────────────────────
  function drawLoss() {
    const [c, w, hh] = draw.fit(lossCanvas);
    const pts = run.points;
    const padX = 12, top = 22, bottom = 14;
    if (!pts.length) {
      c.save(); c.strokeStyle = draw.INK4; c.lineWidth = 1;
      c.beginPath(); c.moveTo(padX, hh - bottom + 0.5); c.lineTo(w - padX, hh - bottom + 0.5); c.stroke(); c.restore();
      return;
    }
    const { lo, hi } = lossDomain(pts);
    const nX = expectedSteps(run);
    const X = (i) => padX + (i / Math.max(1, nX - 1)) * (w - padX * 2);
    const Y = (v) => top + (1 - (Math.log(v) - Math.log(lo)) / (Math.log(hi) - Math.log(lo))) * (hh - top - bottom);
    c.save();
    c.strokeStyle = draw.INK4; c.lineWidth = 1;
    c.beginPath(); c.moveTo(padX, hh - bottom + 0.5); c.lineTo(w - padX, hh - bottom + 0.5); c.stroke();
    c.font = "400 10.5px 'Libre Franklin', sans-serif";
    for (const m of run.marks) {
      const x = Math.round(X(m.i)) + 0.5;
      c.strokeStyle = draw.INK3; c.setLineDash([2, 3]);
      c.beginPath(); c.moveTo(x, 8); c.lineTo(x, hh - bottom); c.stroke();
      c.setLineDash([]); c.fillStyle = draw.INK2; c.fillText(m.label, x + 4, 16);
    }
    c.restore();
    const raw = pts.map((p, i) => [X(i), Y(Math.max(lo, p.loss))]).filter(([, y]) => Number.isFinite(y));
    const best = pts.map((p, i) => [X(i), Y(Math.max(lo, p.best))]).filter(([, y]) => Number.isFinite(y));
    draw.stroke(c, raw, { color: draw.INK3, width: 1 });
    draw.stroke(c, best, { color: draw.INK, width: 1.6, glow: running ? 8 : 0 });
    const last = pts[pts.length - 1];
    if (Number.isFinite(last.best)) {
      const [x, y] = best[best.length - 1];
      c.save(); c.fillStyle = draw.INK2; c.font = "400 10.5px 'Libre Franklin', sans-serif";
      const label = last.best.toFixed(2);
      const tw = c.measureText(label).width;
      c.fillText(label, Math.min(w - tw - 4, x + 6), Math.max(12, y - 6));
      c.restore();
    }
  }
  function drawPlume() {
    const [c, w, hh] = draw.fit(plumeCanvas);
    draw.axis(c, w, hh, { cross: true });
    const tw = run.targetWave, cw = run.phase === "stopped" ? run.bestWave || run.wave : run.wave;
    const lv = plumeLevels(tw, cw);
    if (tw && tw.y?.length) draw.stroke(c, draw.plumePts(w, hh, tw.y, tw.spc, { cycles: 2, level: lv.target }), { color: draw.INK, width: 1.5 });
    if (cw && cw.y?.length) draw.stroke(c, draw.plumePts(w, hh, cw.y, cw.spc, { cycles: 2, level: lv.cand }),
      { color: draw.INK, width: 1.3, dash: [1.5, 4], glow: running ? 8 : 0 });
  }
  function renderAll() {
    const t = phaseText(run, { staticMode, loaded: !!fileBytes });
    word.textContent = t.word;
    detail.textContent = t.detail;
    tag.classList.toggle("mx-hidden", !(recorded && currentRec));
    if (recorded && currentRec) {
      tag.replaceChildren(h("b", { text: "Recorded run" }),
        [currentRec.title, currentRec.recorded ? `recorded ${currentRec.recorded}` : "", currentRec.engine || ""].filter(Boolean).join(" · "));
    }
    const cc = run.phase === "stopped" ? run.bestCC : run.phase === "done" ? run.done?.cc : run.cc;
    if (cc && run.phase !== "idle" && run.phase !== "error") showCC(cc);
    view.dataset.phase = run.phase;
    const done = run.phase === "done" && run.done;
    const stopped = run.phase === "stopped" && !!run.bestCC;
    result.classList.toggle("mx-hidden", !(done || stopped));
    // A stopped run has no closeness and no match render: only its best patch, to load.
    [closeBlock, closeNote, playTarget, playMatch].forEach((el) => el.classList.toggle("mx-hidden", !done));
    if (done) closeNum.textContent = Number.isFinite(run.done.closeness) ? `${Math.round(run.done.closeness)}%` : "–";
    loadBtn.textContent = stopped ? "Load the best so far into the synth" : "Load into the synth";
    playTarget.disabled = !(done && run.done.targetWav);
    playMatch.disabled = !(done && run.done.matchWav);
    loadBtn.disabled = !(run.done?.cc || run.bestCC);
    againBtn.disabled = running || (recorded ? !currentRec : !fileBytes);
    againBtn.textContent = recorded ? "Watch again" : "Match again";
    paintKeys();
    drawLoss();
    drawPlume();
  }

  // ── wiring ──────────────────────────────────────────────────────────────────────
  const ro = new ResizeObserver(() => { drawLoss(); drawPlume(); drawDrop(); });
  [lossCanvas, plumeCanvas, drop].forEach((el) => ro.observe(el));
  disposers.push(() => ro.disconnect());

  buildKeys();
  renderAll();
  if (staticMode) loadIndex();

  return {
    destroy() {
      if (socket) { const ws = socket; socket = null; try { ws.close(); } catch (_) { /* closed */ } }
      replayTimers.forEach(clearTimeout); replayTimers = [];
      if (raf) cancelAnimationFrame(raf);
      for (const [n, t] of auditions) { clearTimeout(t); noteOff(n); }
      stopSound();
      if (audio) { audio.close().catch(() => {}); audio = null; }
      for (const d of disposers.reverse()) { try { d(); } catch (_) { /* keep tearing down */ } }
    },
  };
}
