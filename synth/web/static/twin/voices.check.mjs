// node synth/web/static/twin/voices.check.mjs — exit 0 = the twin's voice count holds.
// The S-1 plays 4 notes at once; in the browser the twin may play 8 or 16. This checks every layer:
//   1. dsp.js Engine: 8 voices sound 8 notes and the 9th steals the oldest; 16 and the 17th; the voice
//      modes (Mono / Unison / Chord) still use the S-1's own voices; the default is still 4.
//   2. worklet.js: processorOptions.voices, the {type:'voices'} rebuild keeps every knob, a stray value
//      is ignored, {type:'stop'} ends the processor.
//   3. audio.js createTwin({voices, destination}) over a fake Web Audio whose node drives the real
//      processor: 8 notes sound, setVoices(4) caps at 4, a bad count is a RangeError.
//   4. core/ctx.js (the page's rule): 8 on the static page, 4 in the cockpit, a saved choice wins, and
//      while an S-1 is linked (or the connected demo) the twin plays 4 whatever the choice.
//   5. The Settings drawer's words.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

import { Engine, S1_VOICES, VOICE_COUNTS, voiceCount } from "./dsp.js";

const curves = JSON.parse(readFileSync(new URL("./curves.json", import.meta.url), "utf8"));
const schema = JSON.parse(readFileSync(new URL("../core/schema.json", import.meta.url), "utf8"));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };

const SR = 48000, B = 128;
const PATCH = { 20: 100, 74: 80, 73: 0, 30: 100, 72: 20 };   // a held saw: every voice sounds while its key is down
const CHORD = [48, 52, 55, 59, 62, 65, 69, 72, 76, 79, 83, 86, 89, 93, 96, 100, 103];
const rms = (x) => Math.sqrt(x.reduce((s, v) => s + v * v, 0) / x.length);
const sounding = (e) => e.voices.filter((v) => v.active && !v.released);
const keys = (e) => sounding(e).map((v) => v.key).sort((a, b) => a - b);
function run(e, blocks) {
  const o = new Float64Array(B), f = new Float64Array(B), a = new Float64Array(B), out = [];
  for (let k = 0; k < blocks; k++) { e.process(o, f, a, B); out.push(...a); }
  return out;
}
/** Each sounding voice's own output over one block (its RMS): a voice that holds a key but is silent fails. */
function voiceLevels(e) {
  return sounding(e).map((v) => {
    const o = new Float64Array(B), f = new Float64Array(B), a = new Float64Array(B);
    v.process(o, f, a, 0, B);
    return rms(a);
  });
}

// ── 1. the engine ────────────────────────────────────────────────────────────────
eq(VOICE_COUNTS, [4, 8, 16], "the twin offers 4, 8 or 16 voices");
ok(S1_VOICES === 4, "the S-1 has 4");
ok(new Engine({ sr: SR, curves }).voices.length === 4, "an Engine with no count has the S-1's 4 (parity renders are unchanged)");
eq([voiceCount(8), voiceCount("16"), voiceCount(6), voiceCount(undefined), voiceCount(null, 8)], [8, 16, 4, 4, 8],
  "voiceCount keeps 4 / 8 / 16 and turns anything else into the fallback");

for (const n of [8, 16]) {
  const e = new Engine({ sr: SR, curves, maxVoices: n });
  e.setAll({ ...PATCH, 80: 2 });
  const notes = CHORD.slice(0, n);
  for (const note of notes) e.noteOn(note);
  run(e, 40);
  eq(keys(e), notes, `poly, ${n} voices: ${n} keys sound ${n} notes`);
  const levels = voiceLevels(e);
  ok(levels.length === n && levels.every((l) => l > 0.01), `every one of the ${n} voices makes sound (min RMS ${Math.min(...levels).toFixed(3)})`);
  e.noteOn(CHORD[n]);
  ok(e.activeVoices === n, `key ${n + 1} finds no free voice: still ${n} voices`);
  eq(keys(e), [...notes.slice(1), CHORD[n]], `key ${n + 1} steals the oldest voice (${notes[0]} stops, ${CHORD[n]} sounds)`);
  const out = run(e, 40);
  ok(out.every(Number.isFinite) && rms(out) > 0.05, `${n} voices render finite, audible output (RMS ${rms(out).toFixed(2)})`);
}
{
  // the S-1's own voice modes are untouched by the extra voices
  const mk = (extra) => { const e = new Engine({ sr: SR, curves, maxVoices: 8 }); e.setAll({ ...PATCH, ...extra }); return e; };
  let e = mk({ 80: 0 });
  for (const note of [48, 52, 55]) e.noteOn(note);
  ok(sounding(e).length === 1 && sounding(e)[0].target === 55, "8 voices, Mono: one voice, the last key");
  e = mk({ 80: 1 });
  e.noteOn(60);
  ok(sounding(e).length === 4 && new Set(sounding(e).map((v) => v.detune)).size === 4, "8 voices, Unison: the S-1's 4 detuned voices");
  e = mk({ 80: 3, 81: 127, 82: 127, 83: 127, 85: 68, 86: 71, 87: 76 });
  e.noteOn(48);
  eq(sounding(e).map((v) => v.target).sort((a, b) => a - b), [48, 52, 55, 60], "8 voices, Chord: the key + 3 chord voices");
  e.noteOn(50);
  eq(sounding(e).map((v) => v.target).sort((a, b) => a - b), [50, 54, 57, 62], "8 voices, Chord: the next key moves the same 4");
}

// ── 2. the worklet ───────────────────────────────────────────────────────────────
globalThis.sampleRate = SR;
globalThis.AudioWorkletProcessor = class { constructor() { this.port = { postMessage() {} }; } };
let Processor = null;
globalThis.registerProcessor = (name, cls) => { if (name === "s1-twin") Processor = cls; };
await import("./worklet.js");
ok(typeof Processor === "function", "worklet.js registers s1-twin");
const outputs = () => Array.from({ length: 5 }, () => [new Float32Array(B)]);
const params = Object.fromEntries(Object.entries(curves.cc_ranges).map(([cc, r]) => [cc, r[2]]));
{
  const p = new Processor({ processorOptions: { curves, params } });
  ok(p.engine.voices.length === 4, "no voices option: the S-1's 4");
  const q = new Processor({ processorOptions: { curves, params, voices: 8 } });
  ok(q.engine.voices.length === 8, "processorOptions.voices = 8: 8 voices");
  const send = (m) => q.port.onmessage({ data: m });
  send({ type: "ccs", values: { ...PATCH, 80: 2 } });
  send({ type: "cc", cc: 74, value: 50 });
  const before = q.engine;
  send({ type: "voices", voices: 16 });
  ok(q.engine !== before && q.engine.voices.length === 16, "{type:'voices', voices:16} rebuilds the voices: 16");
  ok(q.engine.cc.get(74) === 50 && q.engine.cc.get(20) === 100 && q.engine.P.poly === 2, "the rebuild keeps every knob");
  const same = q.engine;
  send({ type: "voices", voices: 16 });
  send({ type: "voices", voices: 6 });
  ok(q.engine === same && q.voices === 16, "the same count, or a count the twin does not offer, changes nothing");
  send({ type: "voices", voices: 4 });
  for (const note of CHORD.slice(0, 5)) send({ type: "on", note, vel: 100 });
  eq(keys(q.engine), CHORD.slice(1, 5), "back to 4 voices: the 5th key steals, like the S-1");
  const outs = outputs();
  ok(q.process([], outs) === true && rms(outs[4][0]) > 0, "process() plays to the 'out' output");
  send({ type: "stop" });
  ok(q.process([], outputs()) === false, "{type:'stop'}: process() returns false, so the browser can retire it");
}

// ── 3. audio.js over a fake Web Audio (its node runs the real processor) ─────────────
const nodes = [];
const fakeNode = (name) => ({ name, connections: [], connect(dst, out = 0) { this.connections.push([dst, out]); return dst; }, disconnect() { this.connections = []; } });
globalThis.AudioWorkletNode = class {
  constructor(ac, name, options) {
    this.options = options;
    this.posted = [];
    this.connections = [];
    this.proc = new Processor({ processorOptions: options.processorOptions });
    const self = this;
    this.port = { postMessage(m) { self.posted.push(m); self.proc.port.onmessage({ data: structuredClone(m) }); } };
    nodes.push(this);
  }
  connect(dst, out = 0) { this.connections.push([dst, out]); return dst; }
  disconnect() { this.connections = []; }
};
globalThis.fetch = async (url) => {
  const u = new URL(String(url));
  if (u.protocol !== "file:") return { ok: false, status: 404, json: async () => ({}) };
  return { ok: true, status: 200, json: async () => JSON.parse(readFileSync(u, "utf8")) };
};
function fakeContext() {
  const added = [];
  return { added, destination: fakeNode("speakers"), audioWorklet: { addModule: async (u) => { added.push(String(u)); } },
    createAnalyser: () => ({ ...fakeNode("analyser"), fftSize: 2048, getFloatTimeDomainData() {} }),
    resume: async () => {}, close: async () => {} };
}
const { createTwin } = await import("./audio.js");
{
  const ac = fakeContext();
  await assert.rejects(createTwin({ curves: "bundled", context: ac, voices: 5 }), RangeError);
  ok(ac.added.length === 0, "createTwin({voices: 5}) is a RangeError, before any audio starts");
  checks++;

  const out = fakeNode("driver gain");
  const twin = await createTwin({ curves: "bundled", context: ac, voices: 8, destination: out });
  const node = nodes.at(-1);
  ok(node.options.processorOptions.voices === 8 && twin.voices === 8, "createTwin({voices: 8}) hands the worklet 8");
  eq(node.connections.filter(([dst]) => dst === out).map(([, k]) => k), [4], "the 'out' stage plays into the destination given");
  ok(!node.connections.some(([dst]) => dst === ac.destination), "and not into the speakers as well");
  twin.setAll({ ...PATCH, 80: 2 });
  for (const note of CHORD.slice(0, 8)) twin.noteOn(note);
  node.proc.process([], outputs());
  eq(keys(node.proc.engine), CHORD.slice(0, 8), "8 keys on the twin sound 8 notes");
  twin.noteOn(CHORD[8]);
  eq(keys(node.proc.engine), CHORD.slice(1, 9), "the 9th steals the oldest");

  const posted = node.posted.length;
  twin.setVoices(8);
  ok(node.posted.length === posted, "setVoices to the same count posts nothing");
  twin.setVoices(4);
  eq(node.posted.at(-1), { type: "voices", voices: 4 }, "setVoices(4) tells the worklet");
  for (const note of CHORD.slice(0, 5)) twin.noteOn(note);
  ok(twin.voices === 4 && sounding(node.proc.engine).length === 4, "and the twin caps at 4, like the S-1");
  ok(node.proc.engine.cc.get(20) === 100, "every knob survived the change");
  assert.throws(() => twin.setVoices(12), RangeError);
  ok(twin.voices === 4, "setVoices(12) is a RangeError and changes nothing");
  checks++;
  const st = await twin.renderStages({ note: 48, seconds: 0.3 });
  ok(st.amp.length > 0 && st.amp.every(Number.isFinite), "renderStages runs on the same voice count");
  await twin.close();
  eq(node.posted.at(-1), { type: "stop" }, "close() ends the processor");
  ok(node.proc.process([], outputs()) === false, "a closed twin's processor stops asking for audio");

  const plain = await createTwin({ curves: "bundled", context: fakeContext() });
  const pnode = nodes.at(-1);
  ok(plain.voices === 4 && pnode.options.processorOptions.voices === 4, "createTwin() with no count: the S-1's 4");
  ok(pnode.connections.some(([dst, k]) => dst.name === "speakers" && k === 4), "with no destination, 'out' goes to the speakers");
}

// ── 4. the page's rule (core/ctx.js) ──────────────────────────────────────────────
const ctxMod = await import("../core/ctx.js");
const { createCtx, VOICE_CHOICES, defaultVoices } = ctxMod;
eq(VOICE_CHOICES, VOICE_COUNTS, "the page offers exactly the counts the twin has");
ok(ctxMod.S1_VOICES === S1_VOICES, "the page and the twin agree the S-1 has 4");
ok(defaultVoices(null) === 8 && defaultVoices({}) === 4, "the default: 8 on the static page, 4 in the cockpit");
function fakeTwin() {
  const calls = [];
  return { calls, set() {}, setAll() {}, noteOn() {}, noteOff() {}, allOff() {}, resume() {}, modeled: () => true,
    setVoices: (n) => calls.push(n) };
}
const transport = { sendParam() {}, sendNote() {} };
{
  const twin = fakeTwin();
  const ctx = createCtx({ schema, twin });
  ok(ctx.voices === 8 && !ctx.voicesLocked && twin.calls.at(-1) === 8, "static page: the twin plays 8");
  const seen = [];
  ctx.on("voices", (e) => seen.push(e));
  ok(ctx.setVoices(16) === true && ctx.voices === 16 && twin.calls.at(-1) === 16, "choosing 16 reaches the twin");
  eq(seen.at(-1), { voices: 16, choice: 16, locked: false }, "and is announced");
  ok(ctx.setVoices(16) === false && ctx.setVoices(5) === false && ctx.voices === 16, "the same count or a bad one changes nothing");
  ctx._setStatus({ sync: "synced", port: "S-1 (demo)", demo: true });
  ok(ctx.voices === 4 && ctx.voicesLocked && twin.calls.at(-1) === 4, "the connected demo pretends an S-1: 4 voices");
}
{
  const twin = fakeTwin();
  const ctx = createCtx({ schema, twin, transport, server: { api() {}, ws() {} } });
  ok(ctx.voices === 4 && ctx.voiceChoice === 4 && twin.calls.at(-1) === 4, "cockpit, no choice yet: the S-1's 4");
  ctx.setVoices(8);
  ok(ctx.voices === 8 && twin.calls.at(-1) === 8, "cockpit with no S-1: the choice plays (8)");
  const seen = [];
  ctx.on("voices", (e) => seen.push(e));
  ctx._setStatus({ sync: "connecting" });
  ok(ctx.voices === 8 && !ctx.voicesLocked, "connecting is not linked yet: still 8");
  ctx._setStatus({ sync: "listening", port: "S-1 MIDI IN" });
  ok(ctx.voices === 4 && ctx.voicesLocked && twin.calls.at(-1) === 4, "an S-1 plugged in (listening): the twin plays 4");
  eq(seen.at(-1), { voices: 4, choice: 8, locked: true }, "the lock is announced, the choice kept");
  const n = twin.calls.length;
  ok(ctx.setVoices(16) === true && ctx.voiceChoice === 16 && ctx.voices === 4 && twin.calls.length === n,
    "choosing 16 while linked is kept for later; the twin stays at 4");
  ctx._setStatus({ sync: "synced" });
  ok(ctx.voices === 4 && twin.calls.length === n, "synced: still 4, nothing re-sent");
  ctx._setStatus({ sync: "disconnected", port: null });
  ok(ctx.voices === 16 && !ctx.voicesLocked && twin.calls.at(-1) === 16, "unplugged: the choice comes back (16)");
}
{
  const saved = createCtx({ schema, twin: fakeTwin(), transport, server: {}, voices: 16 });
  ok(saved.voices === 16, "a saved choice wins over the default");
  const bad = createCtx({ schema, twin: fakeTwin(), voices: 6 });
  ok(bad.voices === 8, "a saved value the page does not offer falls back to the default");
  const stub = { set() {}, setAll() {}, noteOn() {}, noteOff() {}, allOff() {}, modeled: () => true };
  const ctx = createCtx({ schema, twin: stub });
  ctx._setStatus({ sync: "synced" });
  ok(ctx.voices === 4, "a twin with no setVoices (the stand-in) is left alone");
}

// ── 5. the Settings drawer's words ────────────────────────────────────────────────
const { voicesWords } = await import("../drawers/settings.js");
ok(/up to 8 keys/i.test(voicesWords({ voices: 8, choice: 8, locked: false })), "8: says up to 8 keys");
ok(/like the S-1/.test(voicesWords({ voices: 4, choice: 4, locked: false })), "4: says like the S-1");
ok(/connected/.test(voicesWords({ voices: 4, choice: 16, locked: true }))
  && /back to 16/.test(voicesWords({ voices: 4, choice: 16, locked: true })), "linked: says why 4, and what comes back");

console.log(`voices: ${checks} checks passed`);
