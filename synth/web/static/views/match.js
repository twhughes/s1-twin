// views/match.js — the Match view: drop a sound, watch gradient descent find the twin's knobs.
// Contract: docs/design/BUILD.md §2.2 (ctx), §2.4 (/ws/match frames, recorded matches).
// Server mode streams a live match from /ws/match; static mode (ctx.server === null) replays
// recorded runs from matches/index.json with the same visuals. The pure helpers below
// (reduceFrame, phaseText, replaySchedule, lossDomain, …) run in node: views/match.check.mjs.
// Round 2 (docs/design/ROUND2.md §3), server mode only: a target can also be recorded from a
// browser input, or made from the synth's current sound (one note, played by the twin or the
// S-1) as a test, which ends with a report of how many settings the matcher found again.
// Round 7 (W-rec2): the whole take is uploaded and the server finds the sound in it
// (POST /api/match/prepare, synth/match/target_prep.py): the well draws the crop the matcher gets,
// the found notes are marked, "Play target" plays that crop and "Play my patch" plays the marked
// note on the synth. A to K play the synth here too.

import { knob } from "../design/knob.js";
import { seg, GLYPHS } from "../design/seg.js";
import * as draw from "../design/draw.js";
import { rgbOf, lum, noteName } from "../design/colors.js";
import { readHash } from "../core/flags.js";
import { encodeWav, joinChunks, normalize, peakOf } from "../core/wav.js";
import { fitView } from "../core/fit.js";

export const id = "match";
export const title = "Match";
// The bottom strip's key hints on this view (app.js): A to K play the synth here too, so a patch can
// be heard beside the target; the ? list has every key.
export const hints = [
  { key: "A – K", label: "Play" },
  { key: "Z  X", label: "Octave" },
  { key: "Space", label: "Play/pause" },
  { key: "?", label: "Keys" },
];

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
    nsTotal: 0, improved: false, done: null, error: null, trying: null, starts: 0 };
}
/** The words a polish frame carries in "trying" (synth/match/twin_session.py POLISH_WORDS). */
export const POLISH_WORDS = "a final polish";

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
      notes: next.notes, matchWav: f.match_wav_b64 || null, targetWav: f.target_wav_b64 || null, finished: f.finished === true };
    next.trying = null;
    next.bestCC = f.cc || s.bestCC;
    return next;
  }
  const prev = s.points[s.points.length - 1];
  const point = { loss: num(f.loss), best: num(f.best_loss), phase: f.phase, restart: f.restart ?? (prev ? prev.restart : 0),
    polish: f.trying === POLISH_WORDS };
  next.points = [...s.points, point];
  // Leaders on the curve: where a new start begins, where the note search begins, and the polish.
  if (prev && point.phase === "note-search" && prev.phase !== "note-search") {
    next.marks = [...s.marks, { i: next.points.length - 1, label: "Nearby notes" }];
  } else if (prev && point.polish && !prev.polish) {
    next.marks = [...s.marks, { i: next.points.length - 1, label: "Polish" }];
  } else if (prev && point.phase === "gd" && point.restart > 0 && prev.restart !== point.restart) {
    next.marks = [...s.marks, { i: next.points.length - 1, label: `Start ${point.restart + 1}` }];
  }
  // Round 4: a gd frame may say what it is trying (a switch setting, or the polish), and how many starts
  // the run makes; its descents stop when they stop improving, so the step count is open-ended.
  next.trying = f.phase === "gd" && typeof f.trying === "string" && f.trying ? f.trying : null;
  if (Number.isFinite(f.starts)) next.starts = f.starts | 0;
  // The frame that sets (or ties) the best loss carries the best candidate so far.
  if (f.cc && Number.isFinite(point.loss) && point.loss === point.best) { next.bestCC = f.cc; next.bestWave = f.wave || s.bestWave; }
  if (f.phase === "note-search") { next.nsTotal = f.total | 0; next.improved = !!f.improved; next.iter = f.iter | 0; }
  else { next.iter = f.iter | 0; next.total = f.total | 0; next.restart = f.restart | 0; }
  return next;
}

/** The phase in plain words, plus a detail line. `target` (idle, a sound loaded): what the server
 *  found in it, {state: "pending" | "ok" | "failed" | "unavailable", prep, error} (POST /api/match/prepare). */
export function phaseText(s, { staticMode = false, loaded = false, recording = false, target = null } = {}) {
  switch (s.phase) {
    case "idle":
      if (staticMode) return { word: "Choose a recorded run", detail: "" };
      if (recording === "opening") return { word: "Opening the input", detail: "If the browser asks, allow the microphone." };
      if (recording) return { word: "Recording", detail: `Play the sound, then press Stop. It stops by itself at ${RECORD_MAX_S} s.` };
      if (!loaded) return { word: "Waiting for a sound", detail: "" };
      if (target?.state === "failed") return { word: "No clear sound", detail: target.error || "" };
      if (target?.state === "pending") return { word: "Ready to match", detail: "Finding the sound in the take." };
      if (target?.state === "ok" && target.prep) {
        return { word: "Ready to match", detail: `${foundLine(target.prep)}. Check the marked notes, then press Match.` };
      }
      return { word: "Ready to match", detail: "Mark the notes you hear, then press Match." };
    case "connecting": return { word: "Opening the matcher", detail: "" };
    case "pitch": return { word: "Finding the notes", detail: (s.seeded ? "Marked: " : "Found: ") + s.chord };
    case "gd": {
      if (s.starts) {
        return { word: "Descending", detail: `Step ${s.iter}, start ${Math.min(s.restart + 1, s.starts)} of ${s.starts}`
          + (s.trying ? `, trying ${s.trying}` : "") };
      }
      const last = s.total && s.iter >= s.total && s.seeded;
      return { word: "Descending", detail: `Step ${s.iter} of ${s.total}` + (s.restart ? `, start ${s.restart + 1}` : "")
        + (last ? ", choosing the switches" : "") };
    }
    case "note-search": {
      const last = s.nsTotal && s.iter >= s.nsTotal;
      return { word: "Trying nearby notes", detail: s.chord + (s.improved ? ", closer" : "")
        + (last ? ", choosing the switches" : "") };
    }
    case "done": return { word: "Done", detail: (s.chord ? `${s.chord}, ` : "")
      + (s.done ? `${fmtSeconds(s.done.seconds)}, ${s.done.steps} steps` + (s.done.finished ? ", finished early" : "") : "") };
    case "stopped": return { word: "Stopped", detail: s.bestCC ? "The best patch so far is on the knobs." : "" };
    case "error": return { word: s.errorWord || "Could not match", detail: s.error || "" };
    case "making": return { word: "Making the test note",
      detail: s.making ? `${noteName(s.making.note)}, played by ${s.making.source === "s1" ? "the S-1" : "the twin"}` : "" };
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
  // A recorded run may be thinned (tools/thin_match.py): its x axis is the frames it kept, not the
  // steps the live run announced, or 130 kept frames would crowd into a fifth of the well.
  if (s.frameCount) return Math.max(s.points.length, s.frameCount, 2);
  // Round 4: the run stops when it stops improving, so the axis grows with it (from a short start).
  if (s.starts) return Math.max(s.points.length, 40);
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

/** A frame's wave (~64 samples a cycle) as a smooth closed loop for the plume: average the whole
 *  cycles it holds, upsample 4x with a periodic Catmull-Rom, then a light circular smoothing (as the
 *  Synth view's plume does). Returns the cycle tiled three times with {from, spc} on the middle copy,
 *  so drawing one period plus one sample closes the loop; null when there is not a full cycle. */
export function smoothLoop(wave, up = 4) {
  const y = wave?.y, spc = Math.round(wave?.spc || 0);
  if (!y || spc < 8 || y.length < spc) return null;
  const k = Math.max(1, Math.floor(y.length / spc));
  const cyc = new Float32Array(spc);
  for (let c = 0; c < k; c++) for (let j = 0; j < spc; j++) cyc[j] += y[c * spc + j] / k;
  const n = spc * up, fine = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const t = i / up, j = Math.floor(t), u = t - j;
    const p0 = cyc[(j - 1 + spc) % spc], p1 = cyc[j % spc], p2 = cyc[(j + 1) % spc], p3 = cyc[(j + 2) % spc];
    fine[i] = 0.5 * (2 * p1 + (p2 - p0) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u + (3 * p1 - p0 - 3 * p2 + p3) * u * u * u);
  }
  const tmp = new Float32Array(n);
  for (let m = 0; m < 3; m++) {
    for (let i = 0; i < n; i++) tmp[i] = 0.25 * fine[(i - 1 + n) % n] + 0.5 * fine[i] + 0.25 * fine[(i + 1) % n];
    fine.set(tmp);
  }
  const tiled = new Float32Array(n * 3);
  tiled.set(fine, 0); tiled.set(fine, n); tiled.set(fine, 2 * n);
  return { y: tiled, spc: n, from: n };
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

// ── pure: a test of the synth's current sound (ROUND2.md §3) ────────────────────────
export const TEST_NOTE = 48;                      // C3 when no note is marked (driver.PROBE_NOTE)
export const TEST_SECONDS = 2.2, TEST_GATE = 1.2; // key up at 1.2 s, as twin.py renders (2.0 s × 0.6)
export const WITHIN = 10;                         // a knob came back when found within 10 steps
export const RECORD_MAX_S = 8;                    // a recording stops by itself here

const CONTROL = new Map(STAGES.flatMap((s) => s.controls).map((c) => [c.cc, c]));
const SWITCH_CCS = new Set(STAGES.flatMap((s) => s.controls).filter((c) => c.options || c.glyphs).map((c) => c.cc));
const LEVEL_CCS = [20, 19, 21, 23];               // Saw, Square, Sub, Noise
const LFO_CCS = [13, 25, 3, 17, 12];              // Vibrato, LFO amount, Rate, Mod wheel to LFO, Wave
const MATCHER_LFO_WAVES = [2, 3, 0];              // twin.py S_PARAMS lfo_shape: the waves the matcher tries
// twin.py DEFAULT_CURVES for the settings the report reasons about (match.check.mjs: == curves.json);
// the twin's own curves win when given (calibrated ones, once the hardware session has run).
export const CURVE_DEFAULTS = {
  saw_lvl: { lo: 0, hi: 1, kind: "linear" }, square_lvl: { lo: 0, hi: 1, kind: "linear" },
  sub_lvl: { lo: 0, hi: 1, kind: "linear" }, noise_lvl: { lo: 0, hi: 0.5, kind: "linear" },
  cutoff: { lo: 30, hi: 12000, kind: "exp" }, key_follow: { lo: 0, hi: 1, kind: "linear" },
  attack: { lo: 0.001, hi: 2, kind: "exp" }, decay: { lo: 0.005, hi: 4, kind: "exp" },
  sustain: { lo: 0, hi: 1, kind: "linear" }, lfo_to_pitch: { lo: 0, hi: 12, kind: "linear" },
  lfo_to_cutoff: { lo: 0, hi: 4, kind: "linear" }, lfo_depth: { lo: 0, hi: 1, kind: "linear" },
};
const CURVE_OF_CC = { 20: "saw_lvl", 19: "square_lvl", 21: "sub_lvl", 23: "noise_lvl", 74: "cutoff", 26: "key_follow",
  73: "attack", 75: "decay", 30: "sustain", 13: "lfo_to_pitch", 25: "lfo_to_cutoff", 17: "lfo_depth" };
const curveFor = (cc, curves) => curves?.curves?.[CURVE_OF_CC[cc]] || CURVE_DEFAULTS[CURVE_OF_CC[cc]];
/** A knob value 0..127 in the model's units (twin.py Curve.__call__). */
const physical = (c, v) => {
  const k = Math.max(0, Math.min(1, v / 127));
  return c.kind === "exp" ? c.lo * (c.hi / c.lo) ** k : c.lo + (c.hi - c.lo) * k;
};
/** The knob value for a model value (Curve.invert, ×127; may fall outside 0..127). */
const knobFor = (c, x) => 127 * (c.kind === "exp" ? Math.log(x / c.lo) / Math.log(c.hi / c.lo) : (x - c.lo) / (c.hi - c.lo));

/** A value from a Map or a {cc: v} object (number or string keys); undefined when absent. */
function valueIn(params, cc) {
  const v = params && typeof params.get === "function" ? params.get(cc) ?? params.get(String(cc)) : params?.[cc] ?? params?.[String(cc)];
  return v == null || !Number.isFinite(Number(v)) ? undefined : Number(v);
}
const read = (params, cc) => valueIn(params, cc) ?? DEFAULTS[cc];

export const labelOf = (cc) => CONTROL.get(cc)?.label || `CC ${cc}`;
/** A setting's value as its control shows it: a switch's option name (28 → "Gate"), a bipolar
 *  knob's offset from the middle (76: 30 → "−34", as design/knob.js), else the number. */
export function valueText(cc, v) {
  const c = CONTROL.get(cc);
  const opt = c?.options?.find(([value]) => value === v) || c?.glyphs?.find(([value]) => value === v);
  if (opt) return String(opt[opt.length - 1]);
  if (c?.bipolar && Number.isFinite(v)) return (v > 64 ? "+" : v < 64 ? "−" : "") + Math.abs(v - 64);
  return String(v);
}
/** "a", "a and b", "a, b and c". */
export function listText(items) {
  return items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** The 21 twin settings of a param map, defaults filling any gap: the test's true values. */
export function truthOf(params) {
  return Object.fromEntries(TWIN_CCS.map((cc) => [cc, read(params, cc)]));
}

/** The note a test plays: the lowest marked note, else C3. */
export function testNote(seeds) {
  const marked = [...(seeds || [])].filter(Number.isFinite);
  return marked.length ? Math.min(...marked) : TEST_NOTE;
}

/** The pitches one key press sounds, as twin/dsp.js Engine.voicesFor plays them (the matcher is
 *  seeded with these): Range (CC14) shifts octaves; Chord mode (CC80 = 3) adds the chord voices
 *  (CC81–83 on, CC85–87 shifts). `unison`: CC80 = 1, four detuned voices the matcher cannot model. */
export function soundingNotes(note, params) {
  const v = (cc, d) => valueIn(params, cc) ?? d;
  const base = note + 12 * (Math.min(5, Math.max(0, Math.round(v(14, 2)))) - 2);
  const poly = Math.min(3, Math.max(0, Math.round(v(80, 2))));
  const list = [base];
  if (poly === 3) {
    for (const [on, shift] of [[81, 85], [82, 86], [83, 87]]) if (v(on, 127) >= 64) list.push(base + Math.round(v(shift, 64) - 64));
  }
  const notes = [...new Set(list.filter((n) => n >= 0 && n <= 127))].sort((a, b) => a - b).slice(0, 4);
  return { notes: notes.length ? notes : [Math.max(0, Math.min(127, note))], unison: poly === 1 };
}

/** The envelope's level when the key goes up (twin._adsr at TEST_GATE, Envelope mode). */
function envAtKeyUp(params, curves) {
  const A = physical(curveFor(73, curves), read(params, 73));
  const D = physical(curveFor(75, curves), read(params, 75));
  const S = physical(curveFor(30, curves), read(params, 30));
  return (1 - Math.exp((-3 * TEST_GATE) / A)) * (S + (1 - S) * Math.exp(-Math.max(0, TEST_GATE - A) / D));
}

/**
 * Which of the twin's settings shape this test note, by the model's own equations (twin.py render):
 * `ccs` count in the report; `left` lists the rest with why, in plain words. kind "silent": the
 * setting does not change this sound, so it cannot come back. kind "trade": it changes the sound
 * only the way another setting does (Key follow on one note moves the cutoff; Mod wheel to LFO
 * scales both LFO amounts), so only the pair can come back and the report compares the other one.
 * `found` (the matcher's patch), when given, keeps the LFO amounts in when the true sound has no
 * LFO but the found one does: no other row would show that mistake.
 */
export function relevance(params, { notes = [TEST_NOTE], curves = null, found = null } = {}) {
  const v = (cc) => read(params, cc);
  const zero = (cc) => physical(curveFor(cc, curves), v(cc)) <= 1e-9;
  const lfoActs = (get) => [13, 25].some((cc) => physical(curveFor(cc, curves), get(cc)) > 1e-9)
    && physical(curveFor(17, curves), get(17)) > 1e-9;
  const keep = new Set(TWIN_CCS), left = [];
  const out = (ccs, kind, why, name = "") => {
    const gone = ccs.filter((cc) => keep.has(cc));
    if (!gone.length) return;
    gone.forEach((cc) => keep.delete(cc));
    left.push({ ccs: gone, kind, why, name: name || listText(gone.map(labelOf)) });
  };
  if (zero(19)) out([15], "silent", "Square is at 0");
  if (zero(21)) out([22], "silent", "Sub is at 0");
  if (lfoActs(v)) out([17], "trade", "it only scales the LFO amounts");
  else if (found && lfoActs((cc) => read(found, cc))) out([3, 12], "silent", "the true sound has no LFO");
  else out(LFO_CCS, "silent", zero(17) ? "Mod wheel to LFO is at 0" : "Vibrato and LFO amount are at 0", "the LFO");
  const gate = v(28) === 0;
  // In Gate mode the volume still rises and falls with Attack and Release (twin.py: gate = the
  // ADSR with no decay); Decay and Sustain reach the sound only through Env amount.
  if (gate && v(24) === 0) out([75, 30], "silent", "Volume shape is Gate and Env amount is at 0");
  else if (physical(curveFor(30, curves), v(30)) >= 1 - 1e-9) out([75], "silent", "Sustain is full");
  if (!gate && envAtKeyUp(params, curves) < 1e-4) out([72], "silent", "the sound dies away before the key goes up");
  const distinct = [...new Set(notes)];
  if (distinct.length <= 1) {
    if (distinct[0] === 60) out([26], "silent", "at C4 it does nothing");
    else out([26], "trade", "on one note it only moves the cutoff");
  }
  return { ccs: TWIN_CCS.filter((cc) => keep.has(cc)), left };
}
/** The settings that count in the report (ROUND2.md §3): see relevance(). */
export const relevantCCs = (params, opts) => relevance(params, opts).ccs;

/** How the report's rows split into side-by-side tables of at most `per` rows, as evenly as they go:
 *  the tables' height is fixed, so the report fits the panel whatever the count (14 → 7 + 7, 19 → 7 + 6 + 6). */
export function tableSplit(n, per = 7) {
  if (!(n > 0)) return [];
  const k = Math.ceil(n / per), base = Math.floor(n / k), extra = n % k;
  return Array.from({ length: k }, (_, i) => base + (i < extra ? 1 : 0));
}

/**
 * How close a test came back: for each setting that shapes the note, the true value and the found
 * one, compared after the model's exact trades — the levels as a mix (the loss is loudness-blind:
 * twin.spectral_loss normalizes RMS, so only their balance can come back; the found levels are
 * scaled to the true mix's overall level), Cutoff at the true Key follow, the LFO amounts at the
 * true Mod wheel to LFO. Switches count only when they match.
 * opts: {notes, curves, within, source: "twin"|"s1", synced, unison}. Returns
 * {rows, good, total, summary, notes: [sentence]}.
 */
export function recoveryReport(truth, found, { notes = [TEST_NOTE], curves = null, within = WITHIN,
  source = "twin", synced = true, unison = false } = {}) {
  const t = (cc) => read(truth, cc), f = (cc) => read(found, cc);
  const { ccs, left } = relevance(truth, { notes, curves, found });
  const C = (cc) => curveFor(cc, curves);
  const compare = new Map(TWIN_CCS.map((cc) => [cc, f(cc)]));
  const trades = [];
  // the levels: the found mix at the true mix's overall level (a least-squares gain would shrink a
  // mix with none of the true oscillators to nothing, and hide it)
  const tl = LEVEL_CCS.map((cc) => physical(C(cc), t(cc))), fl = LEVEL_CCS.map((cc) => physical(C(cc), f(cc)));
  const norm = (xs) => Math.sqrt(xs.reduce((a, x) => a + x * x, 0));
  if (norm(fl) > 0 && norm(tl) > 0) {
    const g = norm(tl) / norm(fl);
    LEVEL_CCS.forEach((cc, i) => compare.set(cc, knobFor(C(cc), fl[i] * g)));
    trades.push({ ccs: LEVEL_CCS, words: "the levels as a mix (their balance, not their loudness)" });
  }
  if (left.some((l) => l.kind === "trade" && l.ccs.includes(17))) {
    const dT = physical(C(17), t(17)), dF = physical(C(17), f(17));
    for (const cc of [13, 25]) compare.set(cc, knobFor(C(cc), (physical(C(cc), f(cc)) * dF) / dT));
    trades.push({ ccs: [13, 25], words: "the LFO amounts as if Mod wheel to LFO were right" });
  }
  if (left.some((l) => l.kind === "trade" && l.ccs.includes(26))) {
    const shift = ((physical(C(26), f(26)) - physical(C(26), t(26))) * (notes[0] - 60)) / 12;
    compare.set(74, knobFor(C(74), 2 ** (Math.log2(physical(C(74), f(74))) + shift)));
    trades.push({ ccs: [74], words: "Cutoff as if Key follow were right" });
  }
  const rows = ccs.map((cc) => {
    const sw = SWITCH_CCS.has(cc), truthV = t(cc), foundV = f(cc);
    const shown = sw ? foundV : Math.round(compare.get(cc));
    const off = sw ? (foundV === truthV ? 0 : null) : Math.abs(shown - truthV);
    const traded = !sw && shown !== foundV && trades.some((tr) => tr.ccs.includes(cc));
    return { cc, label: labelOf(cc), truth: truthV, found: foundV, shown, off, traded, ok: off !== null && off <= within,
      trueText: valueText(cc, truthV), foundText: valueText(cc, shown) + (traded ? "*" : ""),
      offText: off === null ? "different" : off === 0 ? "same" : `off by ${off}` };
  });
  const good = rows.filter((r) => r.ok).length, total = rows.length;
  const summary = !total ? "No setting shapes this sound."
    : good === total ? `All ${total} settings came back within ${within}.` : `${good} of ${total} settings came back within ${within}.`;
  const said = [];
  const silent = left.filter((l) => l.kind === "silent"), stand = left.filter((l) => l.kind === "trade");
  // Short sentences: the report shares one fixed-height panel with the knobs (views/match.js layout).
  const items = (ls) => ls.map((l) => `${l.name} (${l.why})`).join("; ");   // names can hold "and" themselves
  if (silent.length) said.push(`Left out, as they do not change this sound and so cannot come back: ${items(silent)}.`);
  if (stand.length) said.push(`Also left out: ${items(stand)}.`);
  const usedTrades = trades.filter((tr) => rows.some((r) => r.traded && tr.ccs.includes(r.cc)));
  if (usedTrades.length) said.push(`* Compared as the matcher hears them: ${usedTrades.map((tr) => tr.words).join("; ")}.`);
  if (ccs.includes(12) && !MATCHER_LFO_WAVES.includes(t(12))) {
    said.push(`The matcher tries only Triangle, Square and Saw for the LFO wave, so ${valueText(12, t(12))} cannot come back.`);
  }
  if (unison) said.push("Unison was on: the matcher plays one voice, so this sound cannot come back exactly.");
  if (source === "s1") {
    said.push("From the S-1: the twin is not yet calibrated to it, so a setting can be off even when the sound is close.");
    if (!synced) said.push("No patch was sent to the S-1, so the true values are this app's knobs and may not be its own.");
  }
  return { rows, good, total, summary, notes: said };
}

// ── the view's short copy ────────────────────────────────────────────────────────────
/** Words the one-screen layout (core/fit.js) holds to their lines at 1470 × 760: the left column's notes
 *  to one line of its 330 px, the right column's to one line of about 1,000 px (match.check.mjs). */
export const COPY = {
  intro: "Give it one note or a chord of up to four. The matcher turns the twin's knobs until the twin sounds like it.",
  test: "A test: can the matcher find the knobs as set now?",
  notes: "Mark up to four, or let the matcher find them.",
  search: "Quick: 15 s. Thorough: 2 min. Deep: 5 to 10 min.",
  loss: "The loss, lower is closer. The bright line is the best so far.",
  plume: "Solid: the target. Dotted: the guess.",
  closeness: "Closeness is the app's own measure of how alike the two sound. No one has checked it by ear yet, so trust your ears first.",
  honest: "The matcher finds a patch that sounds like the target, not always the one that made it, and the twin is not yet calibrated to a real S-1.",
};
/** Beside Match, for a test target: its notes, and that it starts from scratch and searches thoroughly. */
export const testGoWords = (notes) => `${notes.map(noteName).join(" + ")}, from scratch: a minute or two`;

// ── pure: recording ─────────────────────────────────────────────────────────────────
/** True for the S-1's USB audio input (CoreAudio calls it "S-1"; synth/audio.py S1_DEVICE_MARKERS). */
export const isS1Label = (label) => /(^|[^a-z0-9])s-1([^a-z0-9]|$)/i.test(String(label || ""));
/** The input to record from: the one the user chose while it is still there, else the S-1, else the default. */
export function pickInput(inputs, chosen = "") {
  if (chosen && inputs.some((d) => d.deviceId === chosen)) return chosen;
  const s1 = inputs.find((d) => isS1Label(d.label));
  if (s1) return s1.deviceId;
  return (inputs.find((d) => d.deviceId === "default") || inputs[0] || { deviceId: "" }).deviceId;
}
/** The picker's options. Until the browser allows recording it hides the names: one default entry. */
export function inputOptions(inputs) {
  if (!inputs.some((d) => d.label)) return [{ value: "", label: "The default input" }];
  return inputs.map((d, i) => ({ value: d.deviceId, label: d.label || `Input ${i + 1}` }));
}
/** A getUserMedia failure, in plain words with what to do. */
export function recordError(e) {
  const name = e?.name || "";
  if (["NotAllowedError", "SecurityError", "PermissionDeniedError"].includes(name)) {
    return "The browser blocked the microphone. Allow it in the address bar, then press Record again.";
  }
  if (["NotFoundError", "OverconstrainedError", "DevicesNotFoundError"].includes(name)) {
    return "No audio input was found. Plug one in (the S-1 is one when it is plugged in), then press Record again.";
  }
  if (["NotReadableError", "TrackStartError", "AbortError"].includes(name)) {
    return "The input did not start. Another app may be using it: close that app, then press Record again.";
  }
  return `Recording did not start${e?.message ? ` (${e.message})` : ""}. Press Record again.`;
}
/** A peak level as 0..1 on a −60..0 dBFS scale: the live recording well. */
export const meterLevel = (peak) => Math.max(0, Math.min(1, (20 * Math.log10(Math.max(peak, 1e-9)) + 60) / 60));

// ── pure: the target the matcher gets (round 7; the server decides, POST /api/match/prepare) ────
/** Seconds as the found line says them: "2.1 s" (to the hundredth when `fine`: a short sound). */
const secs = (t, fine = false) => `${t.toFixed(fine ? 2 : 1)} s`;
/** The one line under a prepared target: "Note C3, held 0.8 s, from 2.1 s to 3.4 s" ("Notes C3 + E3",
 *  and no "held" when the key-up could not be seen). */
export function foundLine(prep) {
  const notes = Array.isArray(prep?.notes) ? prep.notes.filter(Number.isFinite) : [];
  const parts = [];
  if (notes.length) parts.push(`${notes.length > 1 ? "Notes" : "Note"} ${notes.map(noteName).join(" + ")}`);
  if (Number.isFinite(prep?.gate_s)) parts.push(`held ${secs(prep.gate_s)}`);
  const [t0, t1] = Array.isArray(prep?.crop) ? prep.crop : [];
  if (Number.isFinite(t0) && Number.isFinite(t1)) {
    const fine = t1 - t0 < 0.3 || secs(t0) === secs(t1);
    parts.push(`from ${secs(t0, fine)} to ${secs(t1, fine)}`);
  }
  const line = parts.join(", ");
  return line ? line[0].toUpperCase() + line.slice(1) : "The sound";
}
/** The notes to pre-mark on "Which notes?": the ones found, at most four. */
export const foundNotes = (prep) => [...new Set((Array.isArray(prep?.notes) ? prep.notes : [])
  .filter((n) => Number.isInteger(n) && n >= 0 && n <= 127))].sort((a, b) => a - b).slice(0, 4);
/** The keyboard's lowest C (it shows two octaves from there) for these notes: the C at or under the
 *  lowest; `fallback` when there are none. */
export function lowCFor(notes, fallback = 48) {
  if (!notes.length) return fallback;
  const lo = Math.min(...notes);
  return Math.max(0, Math.min(96, lo - (lo % 12)));
}
/** What "Play my patch" plays: the marked notes, else the found ones (at most four). */
export function patchNotes(seeds, prep) {
  const marked = [...(seeds || [])].filter(Number.isFinite).sort((a, b) => a - b);
  return (marked.length ? marked : foundNotes(prep)).slice(0, 4);
}
/** How long "Play my patch" holds its keys: the target's own held time, else its length (a pluck:
 *  the key is simply held while it sounds), 0.3 to 4 s; 1.2 s when nothing is known. */
export function patchHold(prep) {
  if (Number.isFinite(prep?.gate_s) && prep.gate_s > 0) return Math.min(4, Math.max(0.05, prep.gate_s));
  const [t0, t1] = Array.isArray(prep?.crop) ? prep.crop : [];
  if (Number.isFinite(t0) && Number.isFinite(t1) && t1 > t0) return Math.min(4, Math.max(0.3, t1 - t0));
  return TEST_GATE;
}
/** Where the crop lies in the take, as fractions of it: {from, to, keyUp (null if unseen)}. */
export function cropMarks(prep) {
  const d = prep?.duration, [t0, t1] = Array.isArray(prep?.crop) ? prep.crop : [];
  if (!(d > 0) || !Number.isFinite(t0) || !Number.isFinite(t1)) return null;
  const f = (t) => Math.max(0, Math.min(1, t / d));
  const up = Number.isFinite(prep.gate_s) && Number.isFinite(prep.onset) ? prep.onset + prep.gate_s : NaN;
  return { from: f(t0), to: f(t1), keyUp: Number.isFinite(up) && up < t1 ? f(up) : null };
}
/** The server's outline of the take (one 0..1 peak a slice) at `cols` columns, each the loudest it covers. */
export function outlineColumns(peaks, cols) {
  const n = Array.isArray(peaks) ? peaks.length : 0, out = new Float32Array(Math.max(0, cols | 0));
  if (!n || !out.length) return out;
  for (let c = 0; c < out.length; c++) {
    const a = Math.floor((c * n) / out.length), b = Math.max(a + 1, Math.floor(((c + 1) * n) / out.length));
    let m = 0;
    for (let i = a; i < Math.min(n, b); i++) m = Math.max(m, Number(peaks[i]) || 0);
    out[c] = m;
  }
  return out;
}
/** The x of a fraction of the take in a well `w` px wide (design/draw.js maps with PAD = 10; match.check.mjs holds them together). */
export const WELL_PAD = 10;
export const wellX = (w, frac) => WELL_PAD + frac * (w - 2 * WELL_PAD);
/** A marker dragged to `frac` of the take (0..1): the new crop, [t0, t1] seconds, at least 50 ms long. */
export function dragCrop(crop, which, frac, duration, least = 0.05) {
  const t = Math.max(0, Math.min(1, frac)) * duration;
  const [t0, t1] = crop;
  const r = (v) => Math.round(v * 1000) / 1000;
  return which === "from" ? [r(Math.max(0, Math.min(t, t1 - least))), t1] : [t0, r(Math.min(duration, Math.max(t, t0 + least)))];
}

// ── the view ─────────────────────────────────────────────────────────────────────────
const CSS = `
/* One screen (core/fit.js): above 1180 px the view is laid out at 1470 px and must fit 1470 × 760 under the
   header and above the bottom strip at 100% (natural height about 660 px or less, in every state). The right
   column's panel has one fixed height and shows the knobs, or the report after a test. At 1180 px and below
   the view stacks and scrolls. */
.v-match { position: relative; z-index: 1; max-width: 1480px; margin: 0 auto; padding: 12px var(--gutter) 8px; }
.v-match .mx-grid { display: grid; grid-template-columns: minmax(0, 330px) minmax(0, 1fr); gap: var(--gap); align-items: start; }
.v-match .mx-hidden { display: none !important; }
.v-match code { font: 400 12.5px ui-monospace, "SF Mono", Menlo, monospace; color: var(--ink); background: var(--deep); padding: 1px 5px; border-radius: 3px; white-space: nowrap; }

/* the left column: the target, the notes, the search */
.v-match .mx-input > * + * { margin-top: 10px; }
.v-match .mx-input > .mx-recmsg { margin-top: 5px; }
.v-match .mx-listen { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; }
.v-match .mx-input .heading { margin-bottom: 0; }
.v-match .mx-input .note { margin: 3px 0 0; max-width: none; }
.v-match .mx-drop { position: relative; height: 96px; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; text-align: center; padding: 8px 14px; cursor: pointer; outline: 1.25px dashed var(--ink-3); outline-offset: -1px; }
.v-match .mx-drop:hover, .v-match .mx-drop.over { outline-color: var(--ink); }
.v-match .mx-drop:focus-visible { outline: 1.5px dashed var(--ink); outline-offset: 3px; }
.v-match .mx-drop canvas { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.v-match .mx-drop p { position: relative; margin: 0; font-size: 14px; color: var(--ink); }
.v-match .mx-drop .mx-file { font-size: 12.5px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.v-match .mx-drop.loaded, .v-match .mx-drop.recording { justify-content: flex-end; }
.v-match .mx-drop.loaded p.mx-ask, .v-match .mx-drop.recording p.mx-ask { display: none; }
.v-match .mx-drop.recording { outline-style: solid; outline-color: var(--ink); }
.v-match .mx-rec { display: flex; align-items: center; gap: 10px 14px; }
.v-match .mx-rec .pill.on { border-color: var(--ink); }
.v-match .mx-from { display: flex; align-items: center; gap: 8px; flex: 1 1 150px; min-width: 0; font-size: 13px; color: var(--ink-2); }
.v-match .mx-from select { flex: 1; min-width: 0; background: var(--deep); color: var(--ink); border: 1px solid var(--ink-3); border-radius: 3px;
  padding: 7px 8px; font: 400 13.5px var(--sans); }
.v-match .mx-from select:disabled { opacity: .45; }
.v-match .mx-recmsg:empty { display: none; }
.v-match .mx-test { text-align: left; padding: 1px 0; }
.v-match .mx-test:disabled { opacity: .45; cursor: default; color: var(--ink-2); }
.v-match .mx-sub { font: italic 400 19px/1.1 var(--serif); margin: 0; }
.v-match .mx-input > .mx-sub { margin-top: 18px; }
.v-match .mx-keys { position: relative; height: 56px; margin-top: 8px; display: flex; background: var(--deep); border-radius: 3px; user-select: none; -webkit-user-select: none; }
.v-match .mx-keys button { font: inherit; padding: 0; margin: 0; }
.v-match .mx-wk { position: relative; flex: 1; border: 0; border-right: 1px solid var(--ink-4); background: none; cursor: pointer;
  display: flex; align-items: flex-end; justify-content: center; padding-bottom: 4px !important; font-size: 10.5px; color: var(--ink-3); border-radius: 0; }
.v-match .mx-wk.last { border-right: 0; }
.v-match .mx-bk { position: absolute; top: 0; height: 34px; background: var(--field); border: 1px solid var(--ink-3); border-top: 0;
  border-radius: 0 0 2px 2px; cursor: pointer; z-index: 2; color: var(--ink-3); font-size: 10px; }
.v-match .mx-keys [aria-pressed="true"] { background: rgb(var(--pc)); color: var(--on-pc); box-shadow: inset 0 0 0 1.5px var(--ink); }
.v-match .mx-keys.found [aria-pressed="true"] { box-shadow: inset 0 0 0 1.5px var(--ink), inset 0 0 0 3px var(--deep); }
.v-match .mx-keys button:focus-visible { outline-offset: -3px; }
.v-match .mx-keys[aria-disabled="true"] button { cursor: default; }
.v-match .mx-keyrow { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 16px; margin-top: 3px; }
.v-match .mx-keyrow .quiet { padding: 2px 0; }
.v-match .mx-keyrow .mx-marked { color: var(--ink-2); font-size: 13px; margin-right: auto; font-variant-numeric: tabular-nums; }
.v-match .mx-segs { gap: 10px 30px; }
.v-match .mx-go { display: flex; align-items: center; gap: 6px 16px; flex-wrap: wrap; }
.v-match .mx-recorded { list-style: none; margin: 0; padding: 0; }
.v-match .mx-recorded button { background: none; border: 0; border-bottom: 1px solid var(--ink-4); cursor: pointer; width: 100%;
  text-align: left; padding: 8px 0; color: var(--ink-2); font-size: 14.5px; display: flex; justify-content: space-between; gap: 12px; }
.v-match .mx-recorded button:hover, .v-match .mx-recorded button[aria-current="true"] { color: var(--ink); }
.v-match .mx-recorded small { font-size: 12.5px; color: var(--ink-2); font-variant-numeric: tabular-nums; white-space: nowrap; }

/* the right column: the wells, one status row, the panel (knobs or report), one honest line */
.v-match .mx-run { min-width: 0; }
.v-match .mx-wells { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 24px; }
.v-match .mx-wells figure { margin: 0; min-width: 0; }
.v-match .mx-wells canvas { height: 150px; }
.v-match .mx-wells figcaption { margin-top: 7px; font-size: 19px; }
.v-match .mx-wells figcaption .note { display: inline; margin: 0 0 0 10px; font-size: 12.5px; }
.v-match .mx-tag { display: block; margin: 12px 0 -4px; font-size: 12.5px; color: var(--ink-2); }
.v-match .mx-tag b { font-weight: 500; color: var(--ink); border: 1.25px solid var(--ink-3); border-radius: 999px; padding: 2px 9px; margin-right: 8px; }
.v-match .mx-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 26px; margin-top: 12px; min-height: 40px; }
.v-match .mx-status { margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 14px; }
.v-match .mx-word { font: italic 400 24px/1.1 var(--serif); }
.v-match .mx-detail { color: var(--ink-2); font-size: 13.5px; font-variant-numeric: tabular-nums; }
.v-match .mx-result { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 24px; }
.v-match .mx-close { display: flex; align-items: baseline; gap: 9px; }
.v-match .mx-close b { font: 300 36px/1 var(--sans); font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.v-match .mx-close span { font-size: 13.5px; color: var(--ink-2); }
.v-match .mx-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.v-match .mx-run > .mx-closenote, .v-match .mx-run > .mx-warn { margin: 3px 0 0; max-width: none; }
.v-match .mx-panelhead { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 28px; margin-top: 14px; }
.v-match .mx-panelhead .seg .k-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.v-match .mx-panelhead .mx-close b { font-size: 26px; }
.v-match .mx-panel { margin-top: 14px; height: 260px; overflow-y: auto; }
/* four stages side by side, widths set by their controls (Oscillator: 4 dials a row, then 3 and Sub octave) */
.v-match .mx-knobs { display: grid; grid-template-columns: minmax(0, 1.95fr) minmax(0, 1.3fr) minmax(0, 1.3fr) minmax(0, 1fr); gap: 0 22px; }
.v-match .mx-stage h3 { font: italic 400 19px/1.15 var(--serif); margin: 0 0 8px; }
.v-match .mx-stage .row { gap: 10px 8px; }
.v-match .mx-knobs .knob { width: 68px; }
.v-match .mx-knobs .knob svg { width: 44px; height: 44px; }
.v-match .mx-knobs .k-label { margin-top: 3px; white-space: nowrap; }
.v-match .mx-knobs .seg-opts { gap: 4px 7px; }
.v-match .mx-knobs svg, .v-match .mx-knobs .seg-opts button { pointer-events: none; cursor: default; }
.v-match .mx-run > .mx-honest { margin: 10px 0 0; max-width: none; }
.v-match .mx-rtables { display: grid; grid-template-columns: repeat(var(--cols, 2), minmax(0, 1fr)); gap: 0 32px; }
.v-match .mx-rtable { width: 100%; border-collapse: collapse; font-size: 13px; line-height: 1.25; font-variant-numeric: tabular-nums; }
.v-match .mx-rtable th, .v-match .mx-rtable td { font-weight: 400; text-align: right; padding: 3px 0 3px 10px; border-bottom: 1px solid var(--ink-4); white-space: nowrap; }
.v-match .mx-rtable th:first-child { text-align: left; padding-left: 0; white-space: normal; color: var(--ink-2); }
.v-match .mx-rtable thead th { font-size: 11.5px; color: var(--ink-2); padding-top: 0; }
.v-match .mx-rtable td:last-child { color: var(--ink-2); }
.v-match .mx-rtable tr.off th:first-child, .v-match .mx-rtable tr.off td:last-child { color: var(--ink); }
.v-match .mx-rtable tr.off td:last-child { font-weight: 500; }
.v-match .mx-rnotes { margin: 9px 0 0; max-width: none; font-size: 12.5px; line-height: 1.4; }

/* stacked (1180 px and below): one column that scrolls; the panel takes the height it needs */
@media (max-width: 1180px) {
  .v-match { padding: 24px var(--gutter) 48px; }
  .v-match .mx-grid { grid-template-columns: minmax(0, 1fr); }
  .v-match .mx-input .note { max-width: 60ch; }
  .v-match .mx-wells canvas { height: 180px; }
  .v-match .mx-panel { height: auto; overflow: visible; }
  .v-match .mx-knobs { grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 24px 30px; }
  .v-match .mx-rtables { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
}
@media (max-width: 640px) {
  .v-match .mx-wells { grid-template-columns: minmax(0, 1fr); }
  .v-match .mx-wells figcaption .note { display: block; margin: 3px 0 0; }
  .v-match .mx-close b { font-size: 32px; }
  .v-match .mx-rtables { grid-template-columns: minmax(0, 1fr); }
  .v-match .mx-rtable + .mx-rtable thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
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

export default { id, title, mount, unmount, hints };

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
  let finishing = false;        // "finish" was sent; the done frame is on its way
  let replayTimers = [];
  let fileBytes = null, targetBuffer = null, targetLabel = "";
  let quality = "quick", startFrom = "scratch";
  const seeds = new Set();
  let lowC = 48;                // the keyboard shows two octaves from here
  let showRunNotes = true;      // after a run the keyboard shows its notes, until a key is pressed
  let audio = null, source = null;
  let raf = 0;
  let destroyed = false;
  let test = null;              // the loaded target is a test of the current sound: {truth, notes, note, source, synced, unison}
  let making = false;           // the test note is being made (rendered by the twin, or played by the S-1)
  let rec = null;               // a recording in progress (see startRecording)
  let inputs = [], chosenInput = "";
  // What the server found in the loaded target (POST /api/match/prepare): the crop the matcher gets,
  // the key-up, the notes, warnings. state: "none" | "pending" | "ok" | "failed" (no clear sound) |
  // "unavailable" (the server could not say; the match still crops on its own).
  let prep = null, prepState = "none", prepError = "", prepToken = 0;
  let cropOverride = null;      // [t0, t1]: the crop's edges as the user dragged them
  let drag = null;              // a crop mark being dragged: {which: "from" | "to", crop: [t0, t1], moved}
  let swallowClick = false;     // the click that ends a drag is not a click on the well
  let patch = null;             // "Play my patch": {notes, timer} while its keys are held

  // ── layout ──────────────────────────────────────────────────────────────────────
  const view = h("section", { class: "v-match", "aria-label": "Match a sound", "data-phase": "idle" });
  const input = h("div", { class: "mx-input" });
  const runCol = h("div", { class: "mx-run" });
  view.append(h("div", { class: "mx-grid" }, input, runCol));
  root.append(view);
  disposers.push(() => view.remove());
  // one screen: the view's design size, scaled down evenly to fit a smaller window (core/fit.js)
  const fit = fitView(view, { onFit: (s) => { view.dataset.fit = String(s); } });
  disposers.push(() => fit.destroy());
  // A to K play the synth here too (core/keys.js), so a patch can be heard beside the target.
  ctx.keys?.qwerty(true);
  disposers.push(() => ctx.keys?.qwerty(false));

  input.append(
    h("h2", { class: "heading", text: "Match a sound" }),
    h("p", { class: "note", text: staticMode
      ? "The matcher turns the twin's knobs by gradient descent until the twin sounds like a recording. This page cannot run it, so here are real runs, recorded on a computer and replayed step by step."
      : COPY.intro }),
  );

  // server mode: the drop well (or Record, or a test note), the notes, the search budget
  const fileInput = h("input", { type: "file", accept: ".wav,.wave,.aif,.aiff,.flac,.ogg,audio/*", hidden: true, "aria-hidden": "true", tabindex: "-1" });
  const dropCanvas = h("canvas", { "aria-hidden": "true" });
  const dropAsk = h("p", { class: "mx-ask" }, "Drop a sound here, or ",
    h("button", { type: "button", class: "linkish", "data-action": "choose", onclick: (e) => { e.stopPropagation(); if (!busy()) fileInput.click(); } }, "choose a file"));
  const dropFile = h("p", { class: "mx-file" });
  const drop = h("div", { class: "well mx-drop", role: "button", tabindex: "0", "data-role": "drop",
    "aria-label": "Drop a sound here, or press Enter to choose a file" }, dropCanvas, dropAsk, dropFile, fileInput);
  // Listening, before a match: the crop the matcher gets, and the synth's own sound on the marked note.
  const playCrop = h("button", { type: "button", class: "pill", disabled: true, "data-action": "play-crop",
    onclick: () => playBuffer("crop") }, "Play target");
  const playPatchBtn = h("button", { type: "button", class: "pill", disabled: true, "data-action": "play-patch",
    onclick: () => playPatch() }, "Play my patch");
  const resetCrop = h("button", { type: "button", class: "quiet mx-hidden", "data-action": "reset-crop",
    onclick: () => setCrop(null) }, "Reset crop");
  const listenRow = h("div", { class: "mx-listen mx-hidden", "data-role": "listen" }, playCrop, playPatchBtn, resetCrop);
  const recBtn = h("button", { type: "button", class: "pill", "data-action": "record",
    onclick: () => (rec ? stopRecording() : startRecording()) }, "Record");
  const inputSel = h("select", { "data-role": "input", onchange: () => { chosenInput = inputSel.value; } },
    h("option", { value: "" }, "The default input"));
  const recRow = h("div", { class: "mx-rec" }, recBtn, h("label", { class: "mx-from" }, h("span", { text: "From" }), inputSel));
  const recMsg = h("p", { class: "note mx-recmsg", role: "status", "data-role": "record-message" });
  const testBtn = h("button", { type: "button", class: "quiet mx-test", "data-action": "test-current", onclick: () => matchCurrentSound() },
    "Match the synth's current sound");
  const testBox = h("div", { class: "mx-testbox" }, testBtn,
    h("p", { class: "note", text: COPY.test }));

  const keys = h("div", { class: "mx-keys", role: "group", "aria-label": "Notes to match" });
  const marked = h("span", { class: "mx-marked", "aria-live": "polite" });
  const qualitySeg = seg({ label: "Search", value: quality, options: [{ value: "quick", label: "Quick" },
    { value: "thorough", label: "Thorough" }, { value: "deep", label: "Deep" }],
    onInput: (v) => { quality = v; } });
  const startSeg = seg({ label: "Start from", value: startFrom, options: [{ value: "scratch", label: "Scratch" }, { value: "current", label: "Current knobs" }],
    onInput: (v) => { startFrom = v; } });
  const matchBtn = h("button", { type: "button", class: "pill", disabled: true, "data-action": "match", onclick: () => (running ? finishRun() : startMatch()) }, "Match");
  const goNote = h("span", { class: "note", style: "margin:0" });

  if (!staticMode) {
    input.append(
      drop,
      recRow,
      recMsg,
      listenRow,
      testBox,
      h("div", {},
        h("h3", { class: "mx-sub", text: "Which notes?" }),
        h("p", { class: "note", text: COPY.notes }),
        keys,
        h("div", { class: "mx-keyrow" }, marked,
          h("button", { type: "button", class: "quiet", onclick: () => shiftKeys(-12) }, "Lower"),
          h("button", { type: "button", class: "quiet", onclick: () => shiftKeys(12) }, "Higher"),
          h("button", { type: "button", class: "quiet", onclick: () => { seeds.clear(); paintKeys(); syncControls(); } }, "Clear")),
      ),
      h("div", { class: "row mx-segs" }, qualitySeg.el, startSeg.el),
      h("p", { class: "note", text: COPY.search }),
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
        h("code", { text: 'pip install -e ".[studio,twin]"' }), ", then ", h("code", { text: "s1" }), "."),
    );
  }

  // the run: two wells, one status row (the result joins it when done), then one panel of fixed height that
  // shows the knobs, or after a test the report (a switch picks), and one honest line
  const lossCanvas = h("canvas", { class: "well", role: "img", "aria-label": "The loss, falling as the matcher descends" });
  const plumeCanvas = h("canvas", { class: "well", role: "img", "aria-label": "The target and the current guess, drawn as plumes" });
  const word = h("span", { class: "mx-word", "aria-live": "polite" });
  const detail = h("span", { class: "mx-detail" });
  const tag = h("span", { class: "mx-tag mx-hidden", "data-role": "recorded" });
  const closeNum = h("b");
  const closeNote = h("p", { class: "note mx-closenote", text: COPY.closeness });
  const warnLine = h("p", { class: "note mx-warn mx-hidden", role: "status", "data-role": "warnings" });
  const playTarget = h("button", { type: "button", class: "pill", "data-action": "play-target", onclick: () => playBuffer("target") }, "Play target");
  const playMatch = h("button", { type: "button", class: "pill", "data-action": "play-match", onclick: () => playBuffer("match") }, "Play match");
  const loadBtn = h("button", { type: "button", class: "pill", "data-action": "load", onclick: loadIntoSynth }, "Load into the synth");
  const againBtn = h("button", { type: "button", class: "quiet", "data-action": "again", onclick: () => (recorded ? replayAgain() : startMatch()) }, "Match again");
  const closeBlock = h("div", { class: "mx-close" }, closeNum, h("span", { text: "closeness" }));
  const result = h("div", { class: "mx-result mx-hidden", "data-role": "result" },
    closeBlock, h("div", { class: "mx-actions" }, playTarget, playMatch, loadBtn, againBtn));
  // after a test of the current sound: how many settings came back, or the knobs it set
  let panelShows = "report";
  const panelSeg = seg({ label: "Show", value: panelShows,
    options: [{ value: "report", label: "How close it came back" }, { value: "knobs", label: "The knobs" }],
    onInput: (v) => { panelShows = v; paintPanel(); } });
  const reportNum = h("b"), reportWords = h("span");
  const panelHead = h("div", { class: "mx-panelhead mx-hidden", "data-role": "panel-switch" },
    panelSeg.el, h("div", { class: "mx-close", "data-role": "report-summary" }, reportNum, " ", reportWords));
  const reportTables = h("div", { class: "mx-rtables" });
  const reportNotes = h("p", { class: "note mx-rnotes" });
  const report = h("section", { class: "mx-report mx-hidden", "data-role": "report", "aria-label": "How close the test came back" },
    reportTables, reportNotes);
  const knobsBox = h("div", { class: "mx-knobs", "aria-label": "The twin's knobs, as the matcher sets them" });
  runCol.append(
    h("div", { class: "mx-wells" },
      h("figure", {}, lossCanvas, h("figcaption", { class: "caption" }, "Descent",
        h("span", { class: "note", text: COPY.loss }))),
      h("figure", {}, plumeCanvas, h("figcaption", { class: "caption" }, "Target and guess",
        h("span", { class: "note", text: COPY.plume })))),
    tag,
    h("div", { class: "mx-row" }, h("p", { class: "mx-status", role: "status" }, word, detail), result),
    warnLine,
    closeNote,
    panelHead,
    h("div", { class: "mx-panel" }, knobsBox, report),
    h("p", { class: "note mx-honest", text: COPY.honest }),
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
    syncControls();                 // "Play my patch" plays the marked notes
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

  // ── the target: a file, a recording, or a test note, all loaded the same way ─────────
  const busy = () => running || making || !!rec;
  drop.addEventListener("click", () => { if (swallowClick) { swallowClick = false; return; } if (!busy()) fileInput.click(); });
  drop.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && !busy()) { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { const f = e.dataTransfer?.files?.[0]; if (f && !busy()) loadFile(f); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) loadFile(fileInput.files[0]); fileInput.value = ""; });

  async function loadFile(f) {
    if (busy()) return;                  // a run in progress keeps its target
    if (f.size > 25 * 1024 * 1024) { setError("That file is too large (25 MB max). Trim it to a few seconds of the sound."); return; }
    await loadTarget(await f.arrayBuffer(), f.name);
  }
  /** Make `bytes` the target, named `name` in the well ("Recording, 2.4 s"); `testInfo` when it is a test note.
   *  The whole take goes to the server, which finds the sound in it (prepare()); the well then draws the crop. */
  async function loadTarget(bytes, name, testInfo = null) {
    if (running) return;
    fileBytes = bytes;
    targetBuffer = null;
    targetLabel = name;
    test = testInfo;
    run = initialRun(); recorded = false;
    prep = null; prepError = ""; cropOverride = null; decoded.crop = null;
    prepState = staticMode ? "none" : "pending";
    drop.classList.add("loaded");
    dropFile.textContent = name;
    goNote.textContent = test ? testGoWords(test.notes) : "";
    listenRow.classList.remove("mx-hidden");
    syncControls();
    renderAll();
    if (!staticMode) prepare(bytes, ++prepToken);       // not awaited: a test's match starts at once
    try {
      targetBuffer = await ac().decodeAudioData(fileBytes.slice(0));
      targetLabel = `${name}, ${targetBuffer.duration.toFixed(1)} s`;
    } catch (_) {
      targetBuffer = null;               // e.g. AIFF: the browser cannot decode it; the server still can
      if (prep) targetLabel = `${name}, ${prep.duration.toFixed(1)} s`;
    }
    if (!rec && fileBytes === bytes) dropFile.textContent = targetLabel;
    syncControls();
    drawDrop();
  }
  /** Ask the server what the matcher will get from `bytes` (with the user's own edges when `crop`), and
   *  show it: the crop in the well, the found line, the warnings; `mark`: mark the found notes (a new target). */
  async function prepare(bytes, mine, { crop = null, mark = true } = {}) {
    let answer = null, failed = null;
    try {
      const q = crop ? `?crop=${crop[0].toFixed(3)},${crop[1].toFixed(3)}` : "";
      answer = await ctx.server.api("POST", `/api/match/prepare${q}`, bytes, { form: true });
    } catch (e) { failed = e; }
    if (mine !== prepToken || destroyed) return;          // a newer target (or crop) took over
    if (answer && Array.isArray(answer.crop)) {
      prep = answer; prepState = "ok"; decoded.crop = null;
      if (!targetBuffer && fileBytes === bytes) targetLabel = targetLabel.replace(/(, [\d.]+ s)?$/, `, ${prep.duration.toFixed(1)} s`);
      if (mark && !test && !running) {                   // mark what was found; the keys can change it
        seeds.clear();
        foundNotes(prep).forEach((n) => seeds.add(n));
        showRunNotes = false;
        const low = lowCFor([...seeds], lowC);
        if (low !== lowC) { lowC = low; buildKeys(); }
      }
    } else {
      prep = null;
      prepState = failed?.status === 422 ? "failed" : "unavailable";
      prepError = failed?.status === 422 ? failed.message : "";
    }
    if (!rec && fileBytes === bytes) dropFile.textContent = targetLabel;
    syncControls();
    renderAll();
    drawDrop();
  }
  function drawDrop() {
    // the crop on the well itself, for anything that reads the page (a test, a screen reader's label)
    const shown = drag && prep ? drag.crop : prep?.crop;
    drop.dataset.crop = shown ? shown.join(",") : "";
    drop.dataset.duration = prep ? String(prep.duration) : "";
    if (!dropCanvas.isConnected) return;
    const [c, w, hh] = draw.fit(dropCanvas);
    const above = Math.max(20, hh - 22);  // the sound draws above the well's name line
    if (rec && rec.sr) {                 // recording: each 20 ms's peak on a dB scale, drawn up to the record head
      const frac = Math.min(1, rec.total / (RECORD_MAX_S * rec.sr));
      draw.hatchShape(c, w, above, Array.from(rec.bins, meterLevel), { step: 2, reveal: frac });
      draw.playhead(c, w, above, frac);
      return;
    }
    const cols = Math.max(2, Math.floor(w / 2));
    const marks = cropMarks(drag && prep ? { ...prep, crop: drag.crop } : prep);
    const top = prep?.peaks?.length ? outlineColumns(prep.peaks, cols)
      : targetBuffer ? draw.peaksPerColumn(targetBuffer.getChannelData(0), cols) : null;
    if (!top) return;
    if (!marks) { draw.hatchShape(c, w, above, top, { step: 2 }); return; }
    // The take, dimmed; the crop the matcher gets, clear; thin marks at its edges; the key-up, dotted.
    draw.hatchShape(c, w, above, top, { step: 2, alpha: 0.2, edge: draw.INK3 });
    const x0 = wellX(w, marks.from), x1 = wellX(w, marks.to);
    c.save();
    c.beginPath(); c.rect(x0, 0, Math.max(1, x1 - x0), hh); c.clip();
    draw.hatchShape(c, w, above, top, { step: 2 });
    c.restore();
    draw.playhead(c, w, above, marks.from);
    draw.playhead(c, w, above, marks.to);
    if (marks.keyUp != null && wellX(w, marks.keyUp) - x0 > 12) {   // the words only where they fit before the end mark
      draw.keyUpMark(c, w, above, marks.keyUp, x1 - wellX(w, marks.keyUp) > 46 ? "key up" : "");
    }
  }

  // ── the crop's edges, by hand: drag a mark in the well; the server reads the sound between them ──
  /** Where a pointer is in the well: the canvas's own px (the view may be scaled to fit) and the take's fraction. */
  function wellPoint(clientX) {
    const r = dropCanvas.getBoundingClientRect(), w = dropCanvas.clientWidth || 1;
    const x = ((clientX - r.left) / (r.width || 1)) * w;
    return { x, w, frac: (x - WELL_PAD) / Math.max(1, w - 2 * WELL_PAD) };
  }
  /** The mark within reach of the pointer ("from" or "to"), or null. */
  function markNear(clientX) {
    const marks = cropMarks(prep);
    if (!marks || prepState !== "ok" || busy()) return null;
    const { x, w } = wellPoint(clientX);
    const d0 = Math.abs(x - wellX(w, marks.from)), d1 = Math.abs(x - wellX(w, marks.to));
    return Math.min(d0, d1) > 7 ? null : d0 < d1 || (d0 === d1 && x < wellX(w, marks.from)) ? "from" : "to";
  }
  drop.addEventListener("pointerdown", (e) => {
    const which = markNear(e.clientX);
    if (!which) return;
    e.preventDefault();
    drop.setPointerCapture?.(e.pointerId);
    drag = { which, crop: [...prep.crop], moved: false };
  });
  drop.addEventListener("pointermove", (e) => {
    if (!drag) { drop.style.cursor = markNear(e.clientX) ? "ew-resize" : ""; return; }
    drag.crop = dragCrop(drag.crop, drag.which, wellPoint(e.clientX).frac, prep.duration);
    drag.moved = true;
    scheduleDrop();
  });
  const endDrag = (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (e?.type === "pointerup") { swallowClick = true; setTimeout(() => { swallowClick = false; }, 0); }
    if (d.moved && prep && (d.crop[0] !== prep.crop[0] || d.crop[1] !== prep.crop[1])) setCrop(d.crop);
    else drawDrop();
  };
  drop.addEventListener("pointerup", endDrag);
  drop.addEventListener("pointercancel", endDrag);
  /** Use the user's edges (`crop` = [t0, t1] seconds), or the found ones again (null). The notes stay as marked. */
  function setCrop(crop) {
    if (!fileBytes || staticMode || busy()) return;
    cropOverride = crop;
    decoded.crop = null;
    if (crop && prep) prep = { ...prep, crop };           // the new edges show at once; the server's reading follows
    prepState = "pending";
    syncControls(); renderAll(); drawDrop();
    prepare(fileBytes, ++prepToken, { crop, mark: false });
  }

  // ── recording a target from a browser input ────────────────────────────────────────
  // Raw PCM from an AudioWorklet (a ScriptProcessor where there is none), mixed to mono, with the
  // browser's voice processing off: echo cancelling, noise suppression and gain control wreck a
  // synth's sound. Stop ends it; it stops by itself at RECORD_MAX_S. Then: trim to the onset (~5 ms
  // kept), write a WAV (core/wav.js), and load it like a dropped file.
  const REC_BINS = RECORD_MAX_S * 50;    // 20 ms of level each, for the live well
  const RECORDER = `registerProcessor("mx-recorder", class extends AudioWorkletProcessor {
    constructor() {
      super(); this.buf = new Float32Array(2048); this.n = 0; this.on = true;
      this.port.onmessage = () => {
        if (this.n) this.port.postMessage(this.buf.slice(0, this.n));
        this.port.postMessage("done"); this.on = false;
      };
    }
    process(inputs) {
      const ch = inputs[0];
      if (!this.on) return false;
      if (ch && ch.length) {
        for (let i = 0; i < ch[0].length; i++) {
          let s = 0;
          for (let c = 0; c < ch.length; c++) s += ch[c][i];
          this.buf[this.n++] = s / ch.length;
          if (this.n === this.buf.length) { this.port.postMessage(this.buf); this.buf = new Float32Array(2048); this.n = 0; }
        }
      }
      return true;
    }
  });`;
  let recorderURL = null, recorderReady = null;
  const media = () => (typeof navigator !== "undefined" ? navigator.mediaDevices : null);
  const openInput = (id) => media().getUserMedia({ audio: {
    ...(id && id !== "default" ? { deviceId: { exact: id } } : {}),
    echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
  const stopTracks = (stream) => stream?.getTracks().forEach((t) => { try { t.stop(); } catch (_) { /* gone */ } });

  async function refreshInputs() {
    try { inputs = ((await media()?.enumerateDevices?.()) || []).filter((d) => d.kind === "audioinput"); } catch (_) { inputs = []; }
    if (destroyed) return;
    const want = pickInput(inputs, chosenInput);
    inputSel.replaceChildren(...inputOptions(inputs).map((o) => h("option", { value: o.value }, o.label)));
    inputSel.value = inputs.some((d) => d.deviceId === want) ? want : inputSel.options[0]?.value ?? "";
  }
  const onDeviceChange = () => { refreshInputs(); };

  async function recorderNode(A, token) {
    if (A.audioWorklet && typeof AudioWorkletNode === "function") {
      try {
        recorderURL = recorderURL || URL.createObjectURL(new Blob([RECORDER], { type: "text/javascript" }));
        recorderReady = recorderReady || A.audioWorklet.addModule(recorderURL);
        await recorderReady;
        const node = new AudioWorkletNode(A, "mx-recorder", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
        node.port.onmessage = (e) => (e.data === "done" ? token.onDone?.() : onChunk(token, e.data));
        token.worklet = true;
        return node;
      } catch (_) { recorderReady = null; /* the ScriptProcessor below still records */ }
    }
    const node = A.createScriptProcessor(2048, 2, 1);
    node.onaudioprocess = (e) => {
      const b = e.inputBuffer, k = b.numberOfChannels, mono = new Float32Array(b.length);
      for (let c = 0; c < k; c++) { const x = b.getChannelData(c); for (let i = 0; i < x.length; i++) mono[i] += x[i] / k; }
      onChunk(token, mono);
    };
    return node;
  }

  async function startRecording() {
    if (busy() || staticMode) return;
    recMsg.textContent = "";
    if (!media()?.getUserMedia) {
      recMsg.textContent = "This browser cannot record here. Open the app in Chrome, Safari or Firefox on this computer.";
      return;
    }
    const token = rec = { chunks: [], total: 0, bins: new Float32Array(REC_BINS), sr: 0, stream: null, source: null, node: null, sink: null };
    stopSound();
    syncControls(); renderAll();
    try {
      let stream = await openInput(pickInput(inputs, chosenInput));
      token.stream = stream;
      // The input names arrive with the permission: now the S-1 can be found, and preferred.
      await refreshInputs();
      const want = pickInput(inputs, chosenInput);
      const got = stream.getAudioTracks()[0]?.getSettings?.().deviceId;
      if (rec === token && want && want !== "default" && got && want !== got) {
        stopTracks(stream);
        token.stream = stream = await openInput(want);
      }
      if (rec !== token) { stopTracks(stream); return; }      // stopped (or the view closed) while asking
      const A = ac();
      if (A.state === "suspended") await A.resume();
      token.sr = A.sampleRate;
      token.node = await recorderNode(A, token);
      if (rec !== token) { teardownRecording(token); return; }
      token.source = A.createMediaStreamSource(stream);
      token.sink = A.createGain();
      token.sink.gain.value = 0;                              // pulls the recorder; plays nothing
      token.source.connect(token.node); token.node.connect(token.sink); token.sink.connect(A.destination);
      token.guard = setTimeout(stopRecording, (RECORD_MAX_S + 1.5) * 1000);   // in case no audio arrives
      drop.classList.add("recording");
      dropFile.textContent = `Recording, ${fmtSeconds(0)}`;
      syncControls(); renderAll(); drawDrop();
    } catch (e) {
      teardownRecording(token);
      if (rec === token) rec = null;
      recMsg.textContent = recordError(e);
      syncControls(); renderAll();
    }
  }
  function onChunk(token, chunk) {
    if (rec !== token || !token.sr || !chunk?.length) return;
    const max = RECORD_MAX_S * token.sr, take = Math.min(chunk.length, max - token.total);
    if (take <= 0) return;
    const c = take < chunk.length ? chunk.slice(0, take) : chunk;
    token.chunks.push(c);
    const per = max / REC_BINS;
    for (let i = 0; i < c.length; i++) {
      const b = Math.min(REC_BINS - 1, Math.floor((token.total + i) / per)), a = Math.abs(c[i]);
      if (a > token.bins[b]) token.bins[b] = a;
    }
    token.total += take;
    if (!token.stopping) {
      dropFile.textContent = `Recording, ${fmtSeconds(token.total / token.sr)}`;
      scheduleDrop();
      if (token.total >= max) stopRecording();
    }
  }
  let dropRaf = 0;
  const scheduleDrop = () => { if (!dropRaf) dropRaf = requestAnimationFrame(() => { dropRaf = 0; drawDrop(); }); };
  function teardownRecording(token) {
    clearTimeout(token.guard);
    for (const n of [token.source, token.node, token.sink]) { try { n?.disconnect(); } catch (_) { /* gone */ } }
    if (token.node) { if (token.node.port) token.node.port.onmessage = null; else token.node.onaudioprocess = null; }
    stopTracks(token.stream);
  }
  async function stopRecording() {
    const token = rec;
    if (!token || token.stopping) return;
    token.stopping = true;
    if (token.worklet && token.node) {   // the worklet sends what it still holds, then "done"
      await new Promise((resolve) => {
        token.onDone = resolve;
        try { token.node.port.postMessage("stop"); } catch (_) { resolve(); }
        setTimeout(resolve, 400);
      });
    }
    teardownRecording(token);
    if (rec !== token) return;           // the view closed meanwhile
    rec = null;
    drop.classList.remove("recording");
    if (!token.sr) {                     // stopped while the browser was still asking: nothing was recorded
      dropFile.textContent = targetLabel;
      syncControls(); renderAll(); drawDrop();
      return;
    }
    const samples = joinChunks(token.chunks, RECORD_MAX_S * token.sr);
    const peak = peakOf(samples);
    if (peak < 1e-4) {
      recMsg.textContent = "The recording is silent. Check that the input is plugged in and turned up, then press Record again.";
      dropFile.textContent = targetLabel;
      syncControls(); renderAll(); drawDrop();
      return;
    }
    // The whole take goes up: the server finds the sound in it (the lead-in, the room, the click of Stop).
    await loadTarget(encodeWav(samples, token.sr), "Recording");
  }

  // ── a test: match the synth's current sound ────────────────────────────────────────
  // Snapshot the 21 twin settings, make one note with them (the twin renders it, before its
  // browser-only effects; or the S-1 plays it and the cockpit records it), and match it from
  // scratch with the note given: never from the knobs, which would start at the answer.
  async function matchCurrentSound() {
    if (busy() || staticMode) return;
    recMsg.textContent = "";
    const note = testNote(seeds);
    const { notes, unison } = soundingNotes(note, ctx.params);
    const from = ctx.soundSource === "s1" ? "s1" : "twin";
    const info = { truth: truthOf(ctx.params), notes, note, source: from, synced: ctx.status?.link === "synced", unison };
    making = true;
    stopSound();
    run = { ...initialRun(), phase: "making", making: { note, source: from } };
    recorded = false;
    syncControls(); renderAll(); revealRun();
    let bytes;
    try {
      bytes = from === "s1" ? await s1Note(note) : await twinNote(note);
    } catch (e) {
      making = false;
      if (destroyed) return;
      const words = e && (e.status || e.plain) ? e.message
        : from === "s1" ? "Could not reach the app. Check that it is still running, then try again."
          : `The twin could not play the test note${e?.message ? ` (${e.message})` : ""}. Reload the page, then try again.`;
      run = { ...run, phase: "error", errorWord: "Could not make the test note", error: words };
      syncControls(); renderAll();
      return;
    }
    making = false;
    if (destroyed) return;
    await loadTarget(bytes, `${from === "s1" ? "The S-1's" : "The twin's"} current sound, ${noteName(note)}`, info);
    // A test searches at least thoroughly: from scratch, one Quick descent often stops in a wrong valley
    // (Saw traded for Square and Sub), which would grade the matcher on bad luck, not on the model.
    if (quality !== "deep") { quality = "thorough"; qualitySeg.set("thorough"); }
    startMatch();
  }
  const plain = (msg) => Object.assign(new Error(msg), { plain: true });
  async function twinNote(note) {
    const tw = ctx.twin;
    if (!tw || typeof tw.renderStages !== "function" || tw.stub) {
      throw plain("The twin's model did not load in this browser, so it cannot play the test note. Reload the page, then try again.");
    }
    const st = await tw.renderStages({ note, seconds: TEST_SECONDS, gate: TEST_GATE });
    if (!(peakOf(st.amp) >= 1e-4)) {
      throw plain("The synth's current sound is silent: every level is at 0, or the filter or the volume is closed. Turn one up, then try again.");
    }
    return encodeWav(normalize(st.amp, 0.9), st.sr);  // amp: twin.py's model, before the browser-only effects
  }
  async function s1Note(note) {
    const r = await ctx.server.api("POST", "/api/match/record-note", { note, velocity: 100, hold: TEST_GATE, tail: TEST_SECONDS - TEST_GATE });
    return r.arrayBuffer();
  }

  // ── the recovery report (after a test) ─────────────────────────────────────────────
  let reported = null;                   // the done frame the report was built for
  const hasReport = () => !!(test && !recorded && run.phase === "done" && run.done?.cc);
  function renderReport() {
    if (hasReport() && reported !== run.done) {
      reported = run.done;
      const r = recoveryReport(test.truth, run.done.cc, { notes: test.notes, curves: ctx.twin?.curves,
        source: test.source, synced: test.synced, unison: test.unison });
      reportNum.textContent = r.total ? `${r.good} of ${r.total}` : "–";
      reportWords.textContent = r.total ? `settings came back within ${WITHIN}` : "no setting shapes this sound";
      let at = 0;
      const tables = tableSplit(r.rows.length).map((n) => reportTable(r.rows.slice(at, (at += n))));
      reportTables.style.setProperty("--cols", String(Math.max(2, tables.length)));
      reportTables.replaceChildren(...tables);
      reportNotes.textContent = r.notes.join(" ");
      panelShows = "report";              // a new report opens on itself
      panelSeg.set("report");
    }
    paintPanel();
  }
  /** The panel: the report after a test (unless the switch says the knobs), else the knobs. */
  function paintPanel() {
    const has = hasReport(), showReport = has && panelShows === "report";
    panelHead.classList.toggle("mx-hidden", !has);
    report.classList.toggle("mx-hidden", !showReport);
    knobsBox.classList.toggle("mx-hidden", showReport);
  }
  function reportTable(rows) {
    const th = (text) => h("th", { scope: "col", text });
    return h("table", { class: "mx-rtable" },
      h("thead", {}, h("tr", {}, th("Setting"), th("True"), th("Found"), th("Difference"))),
      h("tbody", {}, rows.map((r) => h("tr", { class: r.ok ? "ok" : "off", "data-report-cc": String(r.cc) },
        h("th", { scope: "row", text: r.label }), h("td", { text: r.trueText }), h("td", { text: r.foundText }), h("td", { text: r.offText })))));
  }

  // ── sound (A/B) ─────────────────────────────────────────────────────────────────
  function ac() {
    if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
    return audio;
  }
  function stopSound() { if (source) { try { source.stop(); } catch (_) { /* already stopped */ } source = null; } }
  const decoded = { target: null, match: null, crop: null };
  async function wavBuffer(b64) {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return ac().decodeAudioData(bytes.buffer);
  }
  /** "crop": the crop the matcher gets (the server's, else the whole take while the server cannot say);
   *  "target" and "match": the done frame's A/B. */
  async function playBuffer(which) {
    try {
      const A = ac();
      if (A.state === "suspended") await A.resume();
      let buf = decoded[which];
      if (!buf && which === "crop" && prep?.wav_b64) buf = decoded.crop = await wavBuffer(prep.wav_b64);
      if (!buf && which === "crop" && prepState === "unavailable") buf = targetBuffer;
      if (!buf && which === "target" && targetBuffer && !run.done) buf = targetBuffer;
      if (!buf && run.done && which !== "crop") {
        const b64 = which === "target" ? run.done.targetWav : run.done.matchWav;
        if (b64) buf = decoded[which] = await wavBuffer(b64);
      }
      if (!buf) return;
      stopSound();
      stopPatch();
      source = A.createBufferSource();
      source.buffer = buf;
      const g = A.createGain(); g.gain.value = 0.9;
      source.connect(g); g.connect(A.destination);
      source.start();
    } catch (e) {
      ctx.toast?.("Could not play that sound in this browser.");
    }
  }

  // ── "Play my patch": the synth's own sound (the S-1 when it sounds, else the twin), on the marked
  // note (else the one found), held as long as the target's key was ─────────────────────────
  function playPatch() {
    const notes = patchNotes(seeds, prep);
    if (!notes.length) return;
    stopPatch();
    stopSound();
    try { notes.forEach((n) => ctx.note(n, true, 100)); } catch (_) { /* sound is a courtesy */ }
    patch = { notes, timer: setTimeout(stopPatch, patchHold(prep) * 1000) };
  }
  function stopPatch() {
    if (!patch) return;
    const { notes, timer } = patch;
    patch = null;
    clearTimeout(timer);
    for (const n of notes) { try { ctx.note(n, false); } catch (_) { /* courtesy */ } }
  }

  // ── a live match ────────────────────────────────────────────────────────────────
  function startMatch() {
    if (running || making || rec || !fileBytes || staticMode) return;
    stopSound();
    decoded.target = decoded.match = null;
    run = { ...initialRun(), phase: "connecting" };
    recorded = false;
    showRunNotes = true;
    setRunning(true);
    renderAll();
    revealRun();
    finishing = false;
    // Quick keeps a small pause between frames, so its short run stays watchable; the long searches
    // stream as fast as they compute (the view draws at its own frame rate anyway).
    const q = new URLSearchParams({ throttle: quality === "quick" ? "0.02" : "0", quality });
    // A test gives the matcher its own note and always starts from scratch (the knobs are the answer).
    const notes = test ? test.notes : [...seeds].sort((a, b) => a - b);
    if (notes.length) q.set("notes", notes.join(","));
    if (!test && startFrom === "current") q.set("init", JSON.stringify(initMap(ctx.params)));
    if (cropOverride) q.set("crop", cropOverride.map((t) => t.toFixed(3)).join(","));
    let ws;
    try { ws = ctx.server.ws(`/ws/match?${q}`); } catch (e) { setError("Could not open the matcher. Check that the app is still running, then try again."); return; }
    socket = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { try { ws.send(fileBytes); } catch (_) { setError("Could not send the sound to the matcher. Try again."); } };
    ws.onmessage = (ev) => {
      let f; try { f = JSON.parse(ev.data); } catch (_) { return; }
      apply(f);
      if (f.phase === "done" || f.phase === "error") { socket = null; finishing = false; setRunning(false); try { ws.close(); } catch (_) { /* closed */ } }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      finishing = false;
      if (run.phase !== "done" && run.phase !== "error" && run.phase !== "stopped") {
        setError("The connection to the app closed before the match finished. Check that the app is still running, then try again.");
      }
    };
  }
  /** Stop means finish: the server ends the search at its next step and still sends the done frame
   *  (the best so far, scored, with A/B). With no open socket, stop as before: close, keep the best. */
  function finishRun() {
    if (socket && socket.readyState === 1 && !recorded) {
      try { socket.send("finish"); finishing = true; syncControls(); return; } catch (_) { /* fall back below */ }
    }
    stopRun();
  }
  function stopRun() {
    finishing = false;
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
    syncControls();
    paintKeys();
  }
  /** What each control can do now: a run, a recording and a test note each hold the target. */
  function syncControls() {
    const b = busy();
    matchBtn.textContent = !running ? "Match" : finishing ? "Finishing" : "Finish now";
    matchBtn.disabled = running ? finishing : (!fileBytes || making || !!rec || prepState === "failed");
    playCrop.disabled = !(prep?.wav_b64 || (prepState === "unavailable" && targetBuffer)) || !!rec;
    playPatchBtn.disabled = !patchNotes(seeds, prep).length || !!rec;
    resetCrop.classList.toggle("mx-hidden", !cropOverride);
    resetCrop.disabled = b;
    drop.setAttribute("aria-disabled", String(b));
    recBtn.textContent = rec ? "Stop" : "Record";
    recBtn.classList.toggle("on", !!rec);
    recBtn.disabled = running || making || !!rec?.stopping;
    inputSel.disabled = b;
    testBtn.disabled = b;
    view.dataset.recording = rec ? "1" : "";
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
        h("small", { text: [row.notes.map(noteName).join(" + "), row.closeness != null ? `${Math.round(row.closeness)}% close` : ""].filter(Boolean).join(", ") })))));
    recNote.textContent = recordedRows.length ? "Choose one to watch the matcher work." : "No recorded runs are published here yet.";
    // #match&replay=<slug> starts that run at once: a link that says "watch it find this sound"
    const want = readHash().flags.get("replay");
    const row = typeof want === "string" && recordedRows.find((r) => r.slug === want);
    if (row) playRecorded(row);
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
    run = { ...initialRun(), frameCount: frames.length };
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
    const loop = (wv, level) => {
      const L = smoothLoop(wv);
      return L ? draw.plumePts(w, hh, L.y, L.spc, { from: L.from, cycles: 1 + 1 / L.spc, level })
        : draw.plumePts(w, hh, wv.y, wv.spc, { cycles: 2, level });
    };
    if (tw && tw.y?.length) draw.stroke(c, loop(tw, lv.target), { color: draw.INK, width: 1.5 });
    if (cw && cw.y?.length) draw.stroke(c, loop(cw, lv.cand), { color: draw.INK, width: 1.3, dash: [1.5, 4], glow: running ? 8 : 0 });
  }
  function renderAll() {
    // While recording, the status line speaks for the take (the last run stays until it replaces the target).
    const target = { state: prepState, prep, error: prepError };
    const t = rec ? phaseText(initialRun(), { recording: rec.sr ? true : "opening" })
      : phaseText(run, { staticMode, loaded: !!fileBytes, target });
    word.textContent = t.word;
    detail.textContent = t.detail;
    // what to know about the take, while it waits for a match
    const warns = !rec && run.phase === "idle" && prepState === "ok" ? prep.warnings || [] : [];
    warnLine.textContent = warns.join(" ");
    warnLine.classList.toggle("mx-hidden", !warns.length);
    tag.classList.toggle("mx-hidden", !(recorded && currentRec));
    if (recorded && currentRec) {
      tag.replaceChildren(h("b", { text: "Recorded run" }),
        [currentRec.title, currentRec.recorded ? `recorded ${currentRec.recorded}` : "", currentRec.engine || ""].filter(Boolean).join(", "));
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
    againBtn.disabled = busy() || (recorded ? !currentRec : !fileBytes);
    againBtn.textContent = recorded ? "Watch again" : "Match again";
    renderReport();
    paintKeys();
    drawLoss();
    drawPlume();
  }

  // ── wiring ──────────────────────────────────────────────────────────────────────
  const ro = new ResizeObserver(() => { drawLoss(); drawPlume(); drawDrop(); });
  [lossCanvas, plumeCanvas, drop].forEach((el) => ro.observe(el));
  disposers.push(() => ro.disconnect());

  buildKeys();
  syncControls();
  renderAll();
  if (staticMode) loadIndex();
  else {
    refreshInputs();
    media()?.addEventListener?.("devicechange", onDeviceChange);
    disposers.push(() => media()?.removeEventListener?.("devicechange", onDeviceChange));
  }

  return {
    destroy() {
      destroyed = true;
      if (socket) { const ws = socket; socket = null; try { ws.close(); } catch (_) { /* closed */ } }
      replayTimers.forEach(clearTimeout); replayTimers = [];
      if (raf) cancelAnimationFrame(raf);
      if (dropRaf) cancelAnimationFrame(dropRaf);
      if (rec) { const token = rec; rec = null; teardownRecording(token); }   // the input's light goes off
      if (recorderURL) URL.revokeObjectURL(recorderURL);
      for (const [n, t] of auditions) { clearTimeout(t); noteOff(n); }
      prepToken++;                      // a late answer from the server finds nothing to fill
      stopPatch();
      stopSound();
      if (audio) { audio.close().catch(() => {}); audio = null; }
      for (const d of disposers.reverse()) { try { d(); } catch (_) { /* keep tearing down */ } }
    },
  };
}
