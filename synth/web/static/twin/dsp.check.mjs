// dsp.check.mjs — node checks for the pure twin DSP (exit 0 = pass).
//   node synth/web/static/twin/dsp.check.mjs
// Parity with synth/match/twin.py itself lives in tests/test_twin_parity.py; this file
// checks what parity cannot: finite output at the extremes, no aliasing, determinism,
// the voice modes, and that the real-time path is fast enough to be real time.

import { readFileSync } from 'node:fs';

import { Engine, mergeCurves, physical, renderNote } from './dsp.js';
import { fftPlan } from './fft.js';
import { Fx, master } from './fx.js';

const curves = JSON.parse(readFileSync(new URL('./curves.json', import.meta.url), 'utf8'));
let failures = 0;

function check(name, ok, detail = '') {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures++;
}

/** Run an Engine for `seconds`, returning {osc, filter, amp} as Float64Arrays. */
function runEngine(eng, seconds, events = []) {
  const n = Math.round(seconds * eng.sr);
  const B = 128;
  const out = { osc: new Float64Array(n), filter: new Float64Array(n), amp: new Float64Array(n) };
  const o = new Float64Array(B), f = new Float64Array(B), a = new Float64Array(B);
  let ev = 0;
  for (let pos = 0; pos < n; pos += B) {
    while (ev < events.length && events[ev][0] <= pos) events[ev++][1](eng);
    const m = Math.min(B, n - pos);
    eng.process(o, f, a, m);
    out.osc.set(o.subarray(0, m), pos);
    out.filter.set(f.subarray(0, m), pos);
    out.amp.set(a.subarray(0, m), pos);
  }
  return out;
}

const allFinite = (x) => x.every(Number.isFinite);
const peak = (x) => x.reduce((m, v) => Math.max(m, Math.abs(v)), 0);

// ── 1. finite output at the extremes ─────────────────────────────────────────
{
  const extremes = {
    'everything max': { 20: 127, 19: 127, 21: 127, 23: 127, 15: 127, 74: 127, 71: 127, 24: 127, 25: 127,
      26: 127, 73: 0, 75: 127, 30: 127, 72: 127, 3: 127, 13: 127, 17: 127, 76: 127 },
    'everything min': { 20: 0, 19: 0, 21: 0, 23: 0, 15: 0, 74: 0, 71: 0, 24: 0, 25: 0, 26: 0,
      73: 0, 75: 0, 30: 0, 72: 0, 3: 0, 13: 0, 17: 0, 76: 0 },
    'self-oscillating, closed': { 20: 127, 74: 0, 71: 127, 24: 0, 26: 127 },
    'self-oscillating, swept': { 20: 127, 74: 30, 71: 127, 24: 127, 25: 127, 17: 127, 3: 127 },
    'noise lfo + gate': { 23: 127, 12: 5, 25: 90, 17: 127, 28: 0, 13: 60 },
  };
  for (const [name, cc] of Object.entries(extremes)) {
    for (const note of [0, 36, 84, 127]) {
      const r = renderNote({ cc, curves, note, seconds: 0.5, stages: true });
      const eng = new Engine({ sr: 48000, curves });
      eng.setAll(cc);
      eng.noteOn(note);
      const rt = runEngine(eng, 0.5, [[12000, (e) => e.noteOff(note)]]);
      const ok = allFinite(r.out) && allFinite(r.osc) && allFinite(rt.amp) && allFinite(rt.filter);
      if (!ok || note === 36) check(`finite: ${name}, note ${note}`, ok, `peak ${peak(rt.amp).toFixed(2)}`);
    }
  }
}

// ── 2. band-limiting: no aliasing spikes, even for high notes ─────────────────
function spectrumDb(x) {
  // 4-term Blackman-Harris (-92 dB sidelobes) over the first 2^15 samples
  const N = 1 << 15;
  const re = new Float64Array(N), im = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    const w = 0.35875 - 0.48829 * Math.cos((2 * Math.PI * i) / N) + 0.14128 * Math.cos((4 * Math.PI * i) / N)
      - 0.01168 * Math.cos((6 * Math.PI * i) / N);
    re[i] = x[i] * w;
  }
  fftPlan(N).forward(re, im);
  const db = new Float64Array(N / 2);
  for (let k = 0; k < N / 2; k++) db[k] = 10 * Math.log10(re[k] * re[k] + im[k] * im[k] + 1e-300);
  return db;
}

/** Worst spectral line more than 8 bins from any expected harmonic, in dB re the fundamental. */
function worstSpur(x, sr, f0, H) {
  const db = spectrumDb(x);
  const N = 1 << 15;
  const binHz = sr / N;
  const fundBin = Math.round(f0 / binHz);
  let fund = -Infinity;
  for (let k = fundBin - 3; k <= fundBin + 3; k++) fund = Math.max(fund, db[k]);
  let worst = -Infinity;
  for (let k = 20; k < N / 2; k++) {
    const h = Math.round((k * binHz) / f0);
    const near = h >= 1 && h <= H && Math.abs(k * binHz - h * f0) < 8 * binHz;
    if (!near) worst = Math.max(worst, db[k] - fund);
  }
  return worst;
}

{
  const sr = 48000;
  for (const [label, cc] of [['saw', { 20: 127, 19: 0 }], ['pulse 20%', { 20: 0, 19: 127, 15: 20 }], ['sub asym', { 20: 0, 19: 0, 21: 127, 22: 0 }]]) {
    for (const note of [84, 96, 108]) {
      // the osc stage, band-limited at the audio rate (modelSr = sr) — the hardest case
      const eng = new Engine({ sr, curves, modelSr: sr });
      eng.setAll({ ...cc, 74: 127, 71: 0 });
      eng.noteOn(note);
      const x = runEngine(eng, 0.8).osc;
      // CC76 = 64 is +0.79 cents in the twin (64/127 of -100..+100), so include fine tune
      const cents = physical(cc, curves).fine_tune;
      const f0 = 440 * 2 ** ((note - 69) / 12 + cents / 1200) * (label === 'sub asym' ? 0.25 : 1);
      const spur = worstSpur(x, sr, f0, 10000);
      check(`band-limited: ${label}, note ${note}`, spur < -80, `worst spur ${spur.toFixed(1)} dB`);
    }
  }
  // negative control: a naive (not band-limited) saw at note 96 must fail this test
  const f0 = 440 * 2 ** ((96 - 69) / 12);
  const naive = new Float64Array(38400);
  for (let i = 0; i < naive.length; i++) {
    const ph = ((f0 * i) / sr) % 1;
    naive[i] = 2 * ph - 1;
  }
  const spur = worstSpur(naive, sr, f0, 10000);
  check('band-limit check catches a naive saw (negative control)', spur > -40, `worst spur ${spur.toFixed(1)} dB`);
}

// ── 3. determinism with a seed ────────────────────────────────────────────────
{
  const cc = { 20: 60, 23: 100, 74: 90, 71: 40, 24: 50, 12: 4, 25: 40, 17: 100 };
  const a = renderNote({ cc, curves, note: 48, seconds: 0.5, seed: 0 });
  const b = renderNote({ cc, curves, note: 48, seconds: 0.5, seed: 0 });
  const c = renderNote({ cc, curves, note: 48, seconds: 0.5, seed: 1 });
  let same = true, differs = false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) same = false;
    if (a[i] !== c[i]) differs = true;
  }
  check('offline render is deterministic for a seed', same);
  check('a different seed gives different noise', differs);
  const runs = [0, 1].map(() => {
    const eng = new Engine({ sr: 48000, curves });
    eng.setAll(cc);
    eng.noteOn(48);
    eng.noteOn(55);
    return runEngine(eng, 0.5, [[9600, (e) => e.noteOff(48)]]).amp;
  });
  check('real-time engine is deterministic', runs[0].every((v, i) => v === runs[1][i]));
}

// ── 4. the voice layer ────────────────────────────────────────────────────────
{
  const sr = 48000;
  const mk = (extra) => {
    const e = new Engine({ sr, curves });
    e.setAll({ 20: 100, 74: 80, 73: 0, 30: 100, 72: 20, ...extra });
    return e;
  };
  let e = mk({ 80: 2 });
  for (const n of [48, 52, 55, 59, 62]) e.noteOn(n);
  check('poly: 5 keys on 4 voices steals one', e.activeVoices === 4 && e.voices.some((v) => v.key === 62));
  e = mk({ 80: 0 });
  e.noteOn(48);
  e.noteOn(55);
  e.noteOff(55);
  check('mono: releasing the top key falls back to the held key', e.voices[0].target === 48 && e.activeVoices === 1);
  e = mk({ 80: 3, 81: 127, 82: 127, 83: 0, 85: 64 + 4, 86: 64 + 7, 87: 64 + 12 });
  e.noteOn(48);
  const chord = e.voices.filter((v) => v.active).map((v) => v.target).sort((x, y) => x - y);
  check('chord: CC81-83 switches + CC85-87 shifts', JSON.stringify(chord) === JSON.stringify([48, 52, 55]), chord.join(','));
  e = mk({ 80: 1 });
  e.noteOn(60);
  check('unison: four detuned voices on one key', e.activeVoices === 4 && new Set(e.voices.map((v) => v.detune)).size === 4);
  e = mk({ 80: 0, 31: 2, 5: 90 });
  e.noteOn(48);
  runEngine(e, 0.05);
  e.noteOn(60);
  runEngine(e, 0.02);
  const mid = e.voices[0].pitch;
  check('glide: pitch moves gradually toward the new key', mid > 48.5 && mid < 59.5, `pitch after 20 ms ${mid.toFixed(2)}`);
  e = mk({ 80: 2, 64: 127 });
  e.noteOn(50);
  e.noteOff(50);
  const held = e.voices.some((v) => v.active && !v.released);
  e.set(64, 0);
  const releasedAfter = e.voices.every((v) => !v.active || v.released);
  check('damper holds a released key until the pedal lifts', held && releasedAfter);
  e = mk({ 80: 2, 14: 3 });
  e.noteOn(48);
  check('Range 8\' plays an octave up', e.voices.find((v) => v.active).target === 60);
  // knob moves mid-note (static <-> framed ladders) and a retrigger stay finite and click-free
  e = mk({ 80: 0, 71: 100 });
  e.noteOn(45);
  const events = [9600, 19200, 24000, 28800, 33600, 38400];
  const acts = [(x) => x.set(24, 90), (x) => x.set(25, 80), (x) => x.set(17, 110), (x) => x.set(24, 0),
    (x) => x.set(25, 0), (x) => x.noteOn(52)];
  const r = runEngine(e, 1.0, events.map((t, k) => [t, acts[k]]));
  // A click is a step at the event larger than the waveform's own steps around it.
  const stepMax = (a, b) => {
    let m = 0;
    for (let i = Math.max(1, a); i < Math.min(r.amp.length, b); i++) m = Math.max(m, Math.abs(r.amp[i] - r.amp[i - 1]));
    return m;
  };
  let worst = 0;
  for (const t of events) {
    const at = stepMax(t - 2, t + 3);
    const around = Math.max(stepMax(t - 2400, t - 100), stepMax(t + 100, t + 2400));
    worst = Math.max(worst, at / (around + 1e-9));
  }
  check('knob moves + retrigger mid-note: finite, no clicks', allFinite(r.amp) && worst < 1.25,
    `worst step at an event = ${worst.toFixed(2)} x the waveform's own`);
  check('mergeCurves overlays a calibrated curve', mergeCurves(curves, { calibrated: true, curves: { cutoff: { lo: 40, hi: 9000, kind: 'exp', unit: 'Hz' } } }).curves.cutoff.lo === 40);
}

// ── 5. the effects after the twin: finite, bounded, deterministic ────────────
{
  const sr = 48000, n = sr * 3;
  const x = new Float64Array(n);
  for (let i = 0; i < 4800; i++) x[i] = Math.sin((2 * Math.PI * 440 * i) / sr);
  for (const [label, set] of [['chorus', { 93: 2 }], ['delay', { 92: 127, 90: 60 }],
    ['reverb, longest', { 91: 127, 89: 127 }], ['all at once', { 93: 4, 92: 127, 91: 127, 89: 127 }]]) {
    const runFx = () => {
      const fx = new Fx(sr);
      for (const [c, v] of Object.entries(set)) fx.set(c, v);
      const y = new Float64Array(n);
      fx.process(x, y, n);
      return y;
    };
    const y = runFx(), y2 = runFx();
    const out = new Float64Array(n);
    master(y, out, n);
    check(`effects: ${label} finite, deterministic, output under 1`,
      allFinite(y) && y.every((v, i) => v === y2[i]) && peak(out) <= 1.0, `fx peak ${peak(y).toFixed(3)}`);
  }
}

// ── 6. speed: 4 voices x 1 s of real-time processing at 48 kHz ────────────────
// CPU time is the cost of the code; wall time also counts waiting for a busy machine. The page may ask
// for 16 voices (VOICE_COUNTS), so a 16-note chord gets the same test, plus its slowest 128-sample block
// against the block's own real-time budget (2.67 ms at 48 kHz).
{
  const sr = 48000;
  const busy = { 20: 100, 19: 90, 21: 80, 23: 60, 15: 40, 74: 60, 71: 90, 24: 100, 25: 80, 3: 90, 17: 110,
    13: 20, 73: 5, 75: 60, 30: 80, 72: 60 };
  const chords = { 4: [48, 55, 60, 64], 16: [36, 40, 43, 47, 48, 52, 55, 59, 60, 64, 67, 71, 72, 76, 79, 83] };
  for (const voices of [4, 16]) {
    let cpu = Infinity, wall = Infinity, worst = Infinity;
    for (let run = 0; run < 3; run++) {
      const e = new Engine({ sr, curves, maxVoices: voices });
      e.setAll({ ...busy, 80: 2 });
      for (const n of chords[voices]) e.noteOn(n);
      const B = 128, o = new Float64Array(B), f = new Float64Array(B), a = new Float64Array(B);
      for (let k = 0; k < 400; k++) e.process(o, f, a, B); // let the JIT settle
      const c0 = process.cpuUsage();
      const t0 = performance.now();
      let slowest = 0;
      for (let k = 0; k < sr / B; k++) {
        const b0 = performance.now();
        e.process(o, f, a, B);
        slowest = Math.max(slowest, performance.now() - b0);
      }
      const c = process.cpuUsage(c0);
      wall = Math.min(wall, (performance.now() - t0) / 1000);
      cpu = Math.min(cpu, (c.user + c.system) / 1e6);
      worst = Math.min(worst, slowest);
    }
    console.log(`speed: ${voices} voices x 1 s at 48 kHz (modulated worst case): CPU ${(cpu * 1000).toFixed(0)} ms ` +
      `(ratio ${cpu.toFixed(3)}), wall ${(wall * 1000).toFixed(0)} ms (ratio ${wall.toFixed(3)}), ` +
      `slowest block ${worst.toFixed(2)} ms`);
    check(`${voices} voices: real time with room to spare (CPU ratio < 0.5)`, cpu < 0.5, `ratio ${cpu.toFixed(3)}`);
  }
}

if (failures) {
  console.log(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log('\nall checks passed');
