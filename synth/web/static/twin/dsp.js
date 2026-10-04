// dsp.js — the S-1 twin's signal path in plain JavaScript: the same model as
// synth/match/twin.py, so what the browser plays is what the matcher optimizes.
// PURE module: no DOM, no Web Audio, no fetch. It runs in node, on a page, and
// inside the AudioWorklet (worklet.js).
//
// One set of equations, two renderers:
//
//   renderNote()    offline and exact. A line-by-line port of Twin.render: additive
//                   band-limited oscillators, numpy's seeded noise (rng.js), the
//                   frequency-domain 4-pole ladder with twin.py's own frame layout,
//                   the ADSR/gate VCA. Agrees with Python to ~1e-12.
//   Voice / Engine  real time. Wavetable oscillators (same harmonic-count rule), an
//                   impulse-invariant ladder (the analog ladder's exact matrix
//                   exponential, stepped per sample), the envelope as a state machine
//                   that equals the closed form when knobs hold still, and a cutoff
//                   path that reproduces twin.py's 40 ms frame averaging. Same
//                   equations, different arithmetic: tests/test_twin_parity.py bounds
//                   the difference.
//
// Provenance: every model function below names the twin.py function it ports.

import { filterReal, fftPlan } from './fft.js';
import { NumpyRng } from './rng.js';

export const TWO_PI = 2.0 * Math.PI;
const LN2 = Math.log(2.0);

// ─────────────────────────────────────────────────────────────────────────────
// Small helpers (twin.py module-level functions)
// ─────────────────────────────────────────────────────────────────────────────

/** twin.midi_to_hz. */
export function midiToHz(note) {
  return 440.0 * 2.0 ** ((note - 69.0) / 12.0);
}

/** twin._softplus: max(z,0) + log1p(exp(-|z|)). */
export function softplus(z) {
  return Math.max(z, 0.0) + Math.log1p(Math.exp(-Math.abs(z)));
}

/** twin._harmonic_count: harmonics that fit below Nyquist, with a semitone of headroom. */
export function harmonicCount(f0, sr, maxH = 64) {
  const top = f0 * 1.06;
  if (top <= 0.0) return 1;
  return Math.trunc(Math.max(1, Math.min(maxH, Math.floor((0.5 * sr) / top))));
}

/** Python's round(): half to even (matters for n = round(seconds * sr)). */
export function pyRound(x) {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

function bitLength(v) {
  return v <= 0 ? 0 : 32 - Math.clz32(v);
}

// ─────────────────────────────────────────────────────────────────────────────
// Curves: CC -> k -> physical   (twin.Curve, Twin.cc_to_k, Twin.physical)
// ─────────────────────────────────────────────────────────────────────────────

function requireCurves(curves) {
  if (!curves || !curves.curves || !curves.k_params) {
    throw new Error('dsp: pass the curves JSON (twin/curves.json or /api/twin/curves)');
  }
}

function ccGet(ccMap, cc) {
  if (!ccMap) return undefined;
  if (ccMap instanceof Map) return ccMap.get(cc) ?? ccMap.get(String(cc));
  const v = ccMap[cc];
  return v === undefined ? ccMap[String(cc)] : v;
}

function ccRange(curves, cc) {
  const r = curves.cc_ranges && curves.cc_ranges[String(cc)];
  return r || [0, 127, 0];
}

/** twin.Curve.__call__: linear lo + (hi-lo)k, or exp lo * exp(k ln(hi/lo)). */
export function curveValue(c, k) {
  if (c.kind === 'linear') return c.lo + (c.hi - c.lo) * k;
  if (c.kind === 'exp') return c.lo * Math.exp(k * Math.log(c.hi / c.lo));
  throw new Error(`dsp: unknown curve kind ${c.kind}`);
}

/** Twin.cc_to_k as {name: k in [0,1]} (missing CCs take the s1.json default). */
export function ccToK(ccMap, curves) {
  requireCurves(curves);
  const out = {};
  for (const [name, cc] of curves.k_params) {
    const [lo, hi, def] = ccRange(curves, cc);
    const raw = ccGet(ccMap, cc);
    const num = raw === undefined || raw === null ? def : Number(raw);
    const v = Number.isFinite(num) ? num : def; // a stray value must never reach the audio
    const k = (v - lo) / Math.max(1, hi - lo);
    out[name] = Math.min(1.0, Math.max(0.0, k));
  }
  return out;
}

/** (cc) -> {param: physical value}: the ONLY place units appear (Twin.physical). */
export function physical(ccMap, curves) {
  const k = ccToK(ccMap, curves);
  const out = {};
  for (const [name] of curves.k_params) out[name] = curveValue(curves.curves[name], k[name]);
  return out;
}

/** The discrete s choices (sub octave, LFO shape, amp env mode) from a CC map. */
export function discrete(ccMap, curves) {
  requireCurves(curves);
  const s = { ...(curves.default_s || { sub_octave: 2, lfo_shape: 2, amp_env_mode: 1 }) };
  for (const [name, cc] of curves.s_params) {
    const raw = ccGet(ccMap, cc);
    const v = raw === undefined || raw === null ? NaN : Number(raw);
    if (Number.isFinite(v)) s[name] = Math.round(v);
  }
  // Clamp to the option maps so a stray UI value cannot crash the audio thread.
  const subKeys = Object.keys(curves.sub_octave).map(Number);
  const lfoKeys = Object.keys(curves.lfo_shape).map(Number);
  s.sub_octave = Math.min(Math.max(...subKeys), Math.max(Math.min(...subKeys), s.sub_octave));
  s.lfo_shape = Math.min(Math.max(...lfoKeys), Math.max(Math.min(...lfoKeys), s.lfo_shape));
  return s;
}

/** Merge a (possibly partial, e.g. calibrated) curves JSON over a complete one. */
export function mergeCurves(base, over) {
  if (!over) return base;
  if (!base) return over;
  return { ...base, ...over, curves: { ...base.curves, ...(over.curves || {}) } };
}

// ─────────────────────────────────────────────────────────────────────────────
// Offline, exact: the port of Twin.render
// ─────────────────────────────────────────────────────────────────────────────

/** twin._adsr over n samples (softplus-floored times, exponential attack). */
function adsrArray(A, D, S, R, noteLen, n, sr) {
  const eps = 1e-4;
  A = eps * softplus(A / eps);
  D = eps * softplus(D / eps);
  R = eps * softplus(R / eps);
  const T = noteLen;
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const held = Math.min(Math.max(t, 0.0), T);
    const att = -Math.expm1((-3.0 * held) / A);
    const dec = S + (1.0 - S) * Math.exp(-Math.max(held - A, 0.0) / D);
    const rel = Math.exp(-Math.max(t - T, 0.0) / R);
    out[i] = att * dec * rel;
  }
  return out;
}

/** twin._lfo_wave: the LFO in [-1, 1] (phase 2*pi*rate*t from note start). */
function lfoArray(rate, shape, n, sr) {
  const out = new Float64Array(n);
  const w = TWO_PI * rate;
  if (shape === 'tri') {
    const g = 8.0 / Math.PI ** 2;
    for (let i = 0; i < n; i++) {
      const ph = w * (i / sr);
      let acc = 0.0;
      for (let j = 0, h = 1; h < 18; j++, h += 2) acc += ((-1.0) ** j / (h * h)) * Math.sin(h * ph);
      out[i] = g * acc;
    }
    return out;
  }
  if (shape === 'square') {
    const g = 4.0 / Math.PI;
    for (let i = 0; i < n; i++) {
      const ph = w * (i / sr);
      let acc = 0.0;
      for (let h = 1; h < 18; h += 2) acc += Math.sin(h * ph) / h;
      out[i] = g * acc;
    }
    return out;
  }
  if (shape === 'saw' || shape === 'inv_saw') {
    const g = (shape === 'saw' ? 1.0 : -1.0) * (2.0 / Math.PI);
    for (let i = 0; i < n; i++) {
      const ph = w * (i / sr);
      let acc = 0.0;
      for (let h = 1; h < 12; h++) acc += ((-1.0) ** (h + 1) / h) * Math.sin(h * ph);
      out[i] = g * acc;
    }
    return out;
  }
  // "noise": a fixed slow random control, seeded 1234, stepped evenly over the render
  const rng = new NumpyRng(1234);
  const steps = Math.max(2, pyRound((rate * n) / sr) + 1);
  const raw = new Float64Array(steps);
  for (let j = 0; j < steps; j++) raw[j] = rng.uniform(-1.0, 1.0);
  const nn = Math.max(1, n);
  for (let i = 0; i < n; i++) out[i] = raw[Math.min(steps - 1, Math.floor((i * (steps - 1)) / nn))];
  return out;
}

/** Σ_{h=1..H} a[h] sin(h φ) by Clenshaw's recurrence (one sin/cos per sample). */
function sineSeries(ph, a, H, out, scale) {
  for (let i = 0; i < ph.length; i++) {
    const c2 = 2.0 * Math.cos(ph[i]);
    let b1 = 0.0, b2 = 0.0;
    for (let h = H; h >= 1; h--) {
      const b0 = a[h] + c2 * b1 - b2;
      b2 = b1;
      b1 = b0;
    }
    out[i] = scale * (b1 * Math.sin(ph[i]));
  }
}

/** Σ_{h=1..H} a[h] cos(h φ) by Clenshaw's recurrence. */
function cosineSeries(ph, a, H, out) {
  for (let i = 0; i < ph.length; i++) {
    const c = Math.cos(ph[i]);
    const c2 = 2.0 * c;
    let b1 = 0.0, b2 = 0.0;
    for (let h = H; h >= 1; h--) {
      const b0 = a[h] + c2 * b1 - b2;
      b2 = b1;
      b1 = b0;
    }
    out[i] = b1 * c - b2;
  }
}

/** twin._osc_pulse Fourier amplitudes: (4 / (pi h)) sin(pi h d), d clipped to (0.001, 0.999). */
function pulseAmps(width, H) {
  const d = Math.min(0.999, Math.max(0.001, width));
  const a = new Float64Array(H + 1);
  for (let h = 1; h <= H; h++) a[h] = (4.0 / (Math.PI * h)) * Math.sin(Math.PI * h * d);
  return a;
}

/** twin._ladder_response at rfft bins k*sr/nfft: H = (1+k)/((1 + j f/fc)^4 + k). */
function ladderBins(nfft, sr, fc, k) {
  fc = Math.max(fc, 1e-3);
  k = Math.min(3.98, Math.max(0.0, k));
  const bins = (nfft >> 1) + 1;
  const hRe = new Float64Array(bins);
  const hIm = new Float64Array(bins);
  const val = 1.0 / (nfft * (1.0 / sr)); // numpy rfftfreq spacing
  const num = 1.0 + k;
  for (let b = 0; b < bins; b++) {
    const r = (b * val) / fc;
    // u = 1 + jr; den = ((u*u)*u)*u + k, multiplied left to right like numpy
    let pr = 1.0 - r * r, pi = r + r;
    let t = pr - pi * r;
    pi = pr * r + pi;
    pr = t;
    t = pr - pi * r;
    pi = pr * r + pi;
    pr = t;
    const dr = pr + k, di = pi;
    // numpy complex division (Smith's algorithm) of (num + 0j) by (dr + j di)
    if (Math.abs(dr) >= Math.abs(di)) {
      const rat = di / dr;
      const scl = 1.0 / (dr + di * rat);
      hRe[b] = num * scl;
      hIm[b] = -num * rat * scl;
    } else {
      const rat = dr / di;
      const scl = 1.0 / (di + dr * rat);
      hRe[b] = num * rat * scl;
      hIm[b] = -num * scl;
    }
  }
  return [hRe, hIm];
}

/** twin._ladder_static: one rFFT, zero-padded to >= 2n (linear, not circular). */
function ladderStatic(x, fc, k, sr) {
  const n = x.length;
  const nfft = 2 ** bitLength(Math.max(1, 2 * n - 1));
  const [hRe, hIm] = ladderBins(nfft, sr, fc, k);
  return filterReal(x, nfft, hRe, hIm).subarray(0, n);
}

/** numpy.hanning(M) (symmetric), written the way numpy computes it. */
function hanning(M) {
  const w = new Float64Array(M);
  if (M === 1) {
    w[0] = 1.0;
    return w;
  }
  for (let i = 0; i < M; i++) w[i] = 0.5 + 0.5 * Math.cos((Math.PI * (1 - M + 2 * i)) / (M - 1));
  return w;
}

/** twin._ladder_tv: overlap-add of per-frame static ladders (Hann, 50% overlap, nfft = 4*win, so long ringing never wraps). */
function ladderTV(x, fcT, k, sr, block) {
  const n = x.length;
  block = Math.trunc(Math.max(8, block));
  const win = 2 * block, hop = block, nfft = 4 * win, pad = hop;
  const nFrames = 1 + Math.ceil((pad + n) / hop);
  const total = (nFrames - 1) * hop + win;
  const xp = new Float64Array(total);
  xp.set(x, pad);
  const cp = new Float64Array(total);
  cp.fill(fcT[0], 0, pad);
  cp.set(fcT, pad);
  cp.fill(fcT[n - 1], pad + n);
  const window = hanning(win);
  fftPlan(nfft); // warm the plan once
  const out = new Float64Array((nFrames + nfft / hop - 1) * hop);
  const frame = new Float64Array(win);
  for (let f = 0; f < nFrames; f++) {
    const off = f * hop;
    let sum = 0.0;
    for (let m = 0; m < win; m++) sum += cp[off + m];
    const fcFrame = sum / win;
    for (let m = 0; m < win; m++) frame[m] = xp[off + m] * window[m];
    const [hRe, hIm] = ladderBins(nfft, sr, fcFrame, k);
    const y = filterReal(frame, nfft, hRe, hIm);
    for (let m = 0; m < nfft; m++) out[off + m] += y[m];
  }
  return out.slice(pad, pad + n);
}

/**
 * Twin.render's modulators alone (no oscillators, no FFT): the ADSR env, the LFO (depth
 * applied) and the raw cutoff trajectory in Hz (log2 space + key follow + env/LFO +
 * soft bounds, before the ladder's frame averaging). Cheap enough for any redraw.
 * opts: {cc, curves, note=60, sr=curves.sr, seconds=curves.seconds,
 *        gateFraction=curves.gate_fraction, modelSr=sr} -> {env, lfo, cutoff}.
 */
export function modulators(opts) {
  const curves = opts.curves;
  requireCurves(curves);
  const cc = opts.cc || {};
  const note = opts.note ?? 60;
  const sr = opts.sr ?? curves.sr;
  const seconds = opts.seconds ?? curves.seconds;
  const gateFraction = opts.gateFraction ?? curves.gate_fraction;
  const modelSr = opts.modelSr ?? sr;
  const s = discrete(cc, curves);
  const p = physical(cc, curves);
  const n = Math.max(1, pyRound(seconds * sr));
  const env = adsrArray(p.attack, p.decay, p.sustain, p.release, seconds * gateFraction, n, sr);
  const lfo = lfoArray(p.lfo_rate, curves.lfo_shape[String(s.lfo_shape)], n, sr);
  for (let i = 0; i < n; i++) lfo[i] = p.lfo_depth * lfo[i];
  const keyOct = (p.key_follow * (note - 60)) / 12.0;
  const log2fc = Math.log2(Math.max(p.cutoff, 1e-3)) + keyOct;
  const lo = Math.log2(20.0), hi = Math.log2(0.45 * modelSr);
  const cutoff = new Float64Array(n);
  let modulated = false;
  for (let i = 0; i < n; i++) {
    const mod = p.env_to_cutoff * env[i] + p.lfo_to_cutoff * lfo[i]; // twin: log2_fc + mod
    if (Math.abs(mod) > 1e-6) modulated = true;
    let x = log2fc + mod;
    x = hi - softplus(hi - x);
    x = lo + softplus(x - lo);
    cutoff[i] = 2.0 ** x;
  }
  return { env, lfo, cutoff, modulated };
}

/**
 * Render one note exactly like Twin(sr, seconds, gate_fraction).render(cc_to_k(cc), s, note).
 *
 * opts: {cc, curves, note=60, sr=curves.sr, seconds=curves.seconds,
 *        gateFraction=curves.gate_fraction, seed=0, stages=false, detuneCents=0,
 *        modelSr=sr}
 *
 * `seed` picks the oscillator noise stream (twin.py uses 0). `modelSr` is the rate
 * whose semantics apply (harmonic count, cutoff bounds, noise density, frame length);
 * leave it equal to sr to reproduce Twin(sr=sr).render. Returns a Float64Array, or
 * with stages=true {sr, osc, filter, amp, out, env, lfo, cutoff} (amp === out).
 */
export function renderNote(opts) {
  const curves = opts.curves;
  requireCurves(curves);
  const cc = opts.cc || {};
  const note = opts.note ?? 60;
  const sr = opts.sr ?? curves.sr;
  const seconds = opts.seconds ?? curves.seconds;
  const gateFraction = opts.gateFraction ?? curves.gate_fraction;
  const seed = opts.seed ?? 0;
  const modelSr = opts.modelSr ?? sr;
  const maxH = curves.max_harmonics ?? 64;

  const s = discrete(cc, curves);
  const p = physical(cc, curves);
  const n = Math.max(1, pyRound(seconds * sr));
  const noteLen = seconds * gateFraction;
  const cents = p.fine_tune + (opts.detuneCents || 0);
  const f0 = midiToHz(note);
  const fTop = f0 * 2.0 ** (100.0 / 1200.0);
  const H = Math.min(harmonicCount(fTop, modelSr, maxH), harmonicCount(fTop, sr, maxH));

  // -- modulators (env, LFO, and the cutoff trajectory the ladder follows) --
  const { env, lfo, cutoff, modulated } = modulators({ ...opts, note, sr, seconds, gateFraction, modelSr });

  // -- oscillators (pitch = f0 * detune * LFO vibrato) -------------------
  const ph = new Float64Array(n);
  let acc = 0.0;
  for (let i = 0; i < n; i++) {
    const semis = p.lfo_to_pitch * lfo[i];
    const ratio = Math.exp((cents / 1200.0 + semis / 12.0) * LN2);
    acc += (TWO_PI * (f0 * ratio)) / sr;
    ph[i] = acc;
  }
  const sawA = new Float64Array(H + 1);
  for (let h = 1; h <= H; h++) sawA[h] = (-1.0) ** (h + 1) / h;
  const saw = new Float64Array(n);
  sineSeries(ph, sawA, H, saw, 2.0 / Math.PI);
  const pulse = new Float64Array(n);
  cosineSeries(ph, pulseAmps(p.pulse_width, H), H, pulse);
  const [octShift, subDuty] = curves.sub_octave[String(s.sub_octave)];
  const subPh = new Float64Array(n);
  const octScale = 2.0 ** octShift;
  for (let i = 0; i < n; i++) subPh[i] = ph[i] * octScale;
  const Hs = Math.max(1, Math.floor(H / 2));
  const sub = new Float64Array(n);
  cosineSeries(subPh, pulseAmps(subDuty, Hs), Hs, sub);

  const noiseScale = Math.sqrt(sr / modelSr); // 1 when sr == modelSr (twin.py itself)
  const mix = new Float64Array(n);
  const nzLevel = p.noise_lvl;
  const rng = nzLevel !== 0.0 ? new NumpyRng(seed) : null;
  for (let i = 0; i < n; i++) {
    let v = p.saw_lvl * saw[i] + p.square_lvl * pulse[i] + p.sub_lvl * sub[i];
    if (rng) v += nzLevel * (noiseScale * rng.normal());
    mix[i] = v;
  }

  // -- filter: the 4-pole ladder over that trajectory (twin: static unless modulated) --
  let filtered;
  if (modulated) {
    const blockModel = Math.max(8, Math.floor(modelSr / 50));
    const block = modelSr === sr ? blockModel : Math.round((blockModel * sr) / modelSr);
    filtered = ladderTV(mix, cutoff, p.resonance, sr, block);
  } else {
    filtered = ladderStatic(mix, cutoff[0], p.resonance, sr);
  }

  // -- VCA: full ADSR (Envelope) or held gate (Gate mode) ----------------
  const vca = s.amp_env_mode === 1
    ? env
    : adsrArray(p.attack, p.decay * 0.0 + 1e-3, p.sustain * 0.0 + 1.0, p.release, noteLen, n, sr);
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) out[i] = filtered[i] * vca[i];
  if (!opts.stages) return out;
  return { sr, osc: mix, filter: Float64Array.from(filtered), amp: out, out, env, lfo, cutoff };
}

// ─────────────────────────────────────────────────────────────────────────────
// Real time: shared tables
// ─────────────────────────────────────────────────────────────────────────────

export const TABLE_N = 4096;
let SAW_TABLES = null;

/**
 * 4-point cubic Lagrange read of a guarded table at phase p in [0, 1). Linear reads
 * leave images near -70 dB that land in bands twin.py leaves silent; cubic pushes
 * them below -150 dB, under the log-mel floor the parity gate measures with.
 */
export function readCubic(t, p) {
  const x = p * TABLE_N;
  const i = x | 0;
  const f = x - i;
  const ym = t[i], y0 = t[i + 1], y1 = t[i + 2], y2 = t[i + 3];
  const c1 = y1 - y0 * 0.5 - ym / 3.0 - y2 / 6.0;
  const c2 = 0.5 * (ym + y1) - y0;
  const c3 = (y2 - ym) / 6.0 + 0.5 * (y0 - y1);
  return y0 + f * (c1 + f * (c2 + f * c3));
}

/**
 * Band-limited saw wavetables, one per harmonic count H = 1..maxH:
 * saw_H(p) = (2/pi) Σ_{h<=H} (-1)^(h+1) sin(2 pi h p)/h — twin._osc_saw on one cycle.
 * A pulse of duty d is the difference of two phase-shifted saws with the same H:
 * pulse(p) = saw(p - d/2 + 1/2) - saw(p + d/2 + 1/2) — exactly twin._osc_pulse's series.
 */
export function sawTables(maxH = 64) {
  if (SAW_TABLES && SAW_TABLES.length > maxH) return SAW_TABLES;
  const N = TABLE_N;
  const sinT = new Float64Array(N);
  for (let i = 0; i < N; i++) sinT[i] = Math.sin((TWO_PI * i) / N);
  const acc = new Float64Array(N);
  const tables = [null];
  for (let h = 1; h <= maxH; h++) {
    const c = ((2.0 / Math.PI) * (-1.0) ** (h + 1)) / h;
    for (let i = 0; i < N; i++) acc[i] += c * sinT[(h * i) % N];
    // index k of the cycle lives at t[k + 1]; one guard before, two after (cubic reads)
    const t = new Float32Array(N + 3);
    for (let i = 0; i < N; i++) t[i + 1] = acc[i];
    t[0] = acc[N - 1];
    t[N + 1] = acc[0];
    t[N + 2] = acc[1];
    tables.push(t);
  }
  SAW_TABLES = tables;
  return tables;
}

const LFO_N = 4096;
let LFO_TABLES = null;

/** One cycle of each additive LFO shape of twin._lfo_wave (tri / square / saw). */
export function lfoTables() {
  if (LFO_TABLES) return LFO_TABLES;
  const mk = (fn) => {
    const t = new Float32Array(LFO_N + 1);
    for (let i = 0; i < LFO_N; i++) t[i] = fn((TWO_PI * i) / LFO_N);
    t[LFO_N] = t[0];
    return t;
  };
  const tri = mk((ph) => {
    let acc = 0.0;
    for (let j = 0, h = 1; h < 18; j++, h += 2) acc += ((-1.0) ** j / (h * h)) * Math.sin(h * ph);
    return (8.0 / Math.PI ** 2) * acc;
  });
  const square = mk((ph) => {
    let acc = 0.0;
    for (let h = 1; h < 18; h += 2) acc += Math.sin(h * ph) / h;
    return (4.0 / Math.PI) * acc;
  });
  const saw = mk((ph) => {
    let acc = 0.0;
    for (let h = 1; h < 12; h++) acc += ((-1.0) ** (h + 1) / h) * Math.sin(h * ph);
    return (2.0 / Math.PI) * acc;
  });
  LFO_TABLES = { tri, square, saw };
  return LFO_TABLES;
}

const NOISE_TABLES = new Map();

/** numpy default_rng(seed).standard_normal(len) — the twin's per-note noise, cached. */
export function noiseTable(seed = 0, len = 1 << 17) {
  const key = `${seed}:${len}`;
  let t = NOISE_TABLES.get(key);
  if (!t) {
    const rng = new NumpyRng(seed);
    t = new Float32Array(len);
    for (let i = 0; i < len; i++) t[i] = rng.normal();
    NOISE_TABLES.set(key, t);
  }
  return t;
}

/**
 * The impulse-invariant ladder step for cutoff tau = 2*pi*fc/sr and feedback k.
 *
 * The analog ladder (twin._ladder_response's H(s) = (1+k)/((1+s/wc)^4 + k)) is the
 * state space x' = A x + B u with A = wc(-I + P), P^4 = -k I. So e^{A T} has a closed
 * form: e^{-tau} Σ_{r<4} c_r P^r, c_r = Σ_m (-k)^m tau^(4m+r)/(4m+r)!. Stepping
 * x <- e^{AT}(x + T B u) samples the analog impulse response exactly (impulse
 * invariance), so the magnitude matches H(s) within 0.1 dB below sr/4; above that,
 * aliasing of the 24 dB/oct tail adds up to +3 dB at 0.44*sr (at 48 kHz: above 19 kHz).
 * Writes [d0, d1, d2, d3, g] into out: Phi = Toeplitz(d, -k d), g = tau(1+k).
 */
export function ladderCoefs(tau, k, out) {
  let term = 1.0, c0 = 0.0, c1 = 0.0, c2 = 0.0, c3 = 0.0, sgn = 1.0;
  for (let m = 0; m < 16; m++) {
    const j = 4 * m;
    c0 += sgn * term;
    term *= tau / (j + 1);
    c1 += sgn * term;
    term *= tau / (j + 2);
    c2 += sgn * term;
    term *= tau / (j + 3);
    c3 += sgn * term;
    term *= tau / (j + 4);
    sgn *= -k;
    if (Math.abs(sgn * term) < 1e-18 || sgn === 0) break;
  }
  const E = Math.exp(-tau);
  out[0] = E * c0;
  out[1] = E * c1;
  out[2] = E * c2;
  out[3] = E * c3;
  out[4] = tau * (1.0 + k);
  return out;
}

// ─────────────────────────────────────────────────────────────────────────────
// Real time: parameters, the Voice, and the Engine (voice allocation)
// ─────────────────────────────────────────────────────────────────────────────

const CONTROL = 16; // samples between pitch-table / static-ladder checks
const FM_STEP = 8; // sample step of a frame's mean-cutoff estimate
const FRAME_SPAN = 8; // hops a framed ladder lives (2 of input + 6 of ringing)
// Unison detune spread in cents. The twin does not model unison: an uncalibrated guess.
export const UNISON_DETUNE = [-9, -3, 3, 9];

/** The S-1 plays 4 notes at once: the twin's voice count whenever it stands in for an S-1. */
export const S1_VOICES = 4;
/**
 * The voice counts a page may ask for (worklet.js, audio.js). More than the S-1's 4 is a
 * browser extra, like the effects: each voice is still twin.py's one-note model.
 */
export const VOICE_COUNTS = [4, 8, 16];

/** `n` when it is one of VOICE_COUNTS, else `fallback`: a stray value never reaches the audio thread. */
export function voiceCount(n, fallback = S1_VOICES) {
  const v = Number(n);
  return VOICE_COUNTS.includes(v) ? v : fallback;
}

/** Everything a Voice reads, computed once per CC change. */
function voiceParams(ccMap, curves) {
  const p = physical(ccMap, curves);
  const s = discrete(ccMap, curves);
  const eps = 1e-4;
  const cc = (n) => {
    const v = ccGet(ccMap, n);
    return v === undefined || v === null ? ccRange(curves, n)[2] : Number(v);
  };
  const [subShift, subDuty] = curves.sub_octave[String(s.sub_octave)];
  const glideMode = Math.round(cc(31)); // 0 Off, 1 Auto (legato), 2 On
  return {
    saw: p.saw_lvl,
    square: p.square_lvl,
    sub: p.sub_lvl,
    noise: p.noise_lvl,
    pw: Math.min(0.999, Math.max(0.001, p.pulse_width)),
    cutoff: p.cutoff,
    res: Math.min(3.98, Math.max(0.0, p.resonance)),
    envAmt: p.env_to_cutoff,
    lfoCut: p.lfo_to_cutoff,
    keyFollow: p.key_follow,
    A: eps * softplus(p.attack / eps),
    D: eps * softplus(p.decay / eps),
    S: p.sustain,
    R: eps * softplus(p.release / eps),
    lfoRate: p.lfo_rate,
    lfoPitch: p.lfo_to_pitch,
    lfoDepth: p.lfo_depth,
    cents: p.fine_tune,
    lfoShape: curves.lfo_shape[String(s.lfo_shape)],
    subShift,
    subDuty: Math.min(0.999, Math.max(0.001, subDuty)),
    gateMode: s.amp_env_mode !== 1,
    // twin.render: the time-varying (framed) ladder runs iff the cutoff modulation is ever > 1e-6
    modulated: Math.abs(p.env_to_cutoff) > 1e-6 || Math.abs(p.lfo_to_cutoff * p.lfo_depth) > 1e-6,
    // -- the voice layer (browser behavior around the model, not twin.py) --
    poly: Math.min(3, Math.max(0, Math.round(cc(80)))), // Mono / Unison / Poly / Chord
    chordOn: [cc(81) >= 64, cc(82) >= 64, cc(83) >= 64],
    chordShift: [cc(85) - 64, cc(86) - 64, cc(87) - 64].map(Math.round),
    glide: glideMode === 0 && cc(65) >= 64 ? 2 : glideMode,
    // Portamento time: an uncalibrated guess, 1 ms .. 2 s time constant.
    glideTau: 0.001 * 2000 ** (Math.min(127, Math.max(0, cc(5))) / 127),
    rangeShift: 12 * (Math.min(5, Math.max(0, Math.round(cc(14)))) - 2), // Range: 16' = as played
    damper: cc(64) >= 64,
  };
}

/** A ladder slot. One shape for every slot keeps the JIT's property access monomorphic. */
function newSlot() {
  return { on: false, frame: 0, w0: 0, end: 0, inputOff: false, rise: -Infinity, fall: Infinity,
    x1: 0, x2: 0, x3: 0, x4: 0, d0: 0, d1: 0, d2: 0, d3: 0, g: 0, k1: 0, k2: 0, k3: 0, fc: 0 };
}

/** Set a ladder slot's coefficients for cutoff fc (Hz) and feedback k. */
function setSlot(sl, fc, k, sr, tmp) {
  sl.fc = fc;
  ladderCoefs((TWO_PI * Math.min(fc, 0.49 * sr)) / sr, k, tmp);
  sl.d0 = tmp[0];
  sl.d1 = tmp[1];
  sl.d2 = tmp[2];
  sl.d3 = tmp[3];
  sl.g = tmp[4];
  sl.k1 = -k * tmp[1];
  sl.k2 = -k * tmp[2];
  sl.k3 = -k * tmp[3];
}

/** One sample through a ladder slot: returns x4, then x <- Phi (x + g u e1). */
function stepSlot(sl, u) {
  const y = sl.x4;
  const s1 = sl.x1 + sl.g * u;
  const x2 = sl.x2, x3 = sl.x3;
  const d0 = sl.d0, d1 = sl.d1, d2 = sl.d2, d3 = sl.d3, k1 = sl.k1, k2 = sl.k2, k3 = sl.k3;
  sl.x1 = d0 * s1 + k3 * x2 + k2 * x3 + k1 * y;
  sl.x2 = d1 * s1 + d0 * x2 + k3 * x3 + k2 * y;
  sl.x3 = d2 * s1 + d1 * x2 + d0 * x3 + k3 * y;
  sl.x4 = d3 * s1 + d2 * x2 + d1 * x3 + d0 * y;
  return y;
}

/**
 * One real-time voice: the twin's equations for one note, sample by sample. Built and
 * driven by an Engine (which owns the shared tables and the CC state); the worklet uses
 * Engine, so a Voice alone is mostly useful for tests.
 */
export class Voice {
  constructor(engine, index) {
    this.e = engine;
    this.index = index;
    this.active = false;
    this.released = false;
    this.tmp = new Float64Array(5);
    // Framed ladders: a frame takes input for 2 hops, then rings on its own coefficients.
    // twin._ladder_tv's nfft = 4 hops wraps longer tails onto the frame start; here each
    // frame rings for FRAME_SPAN hops instead (twin's math without the wrap), so up to
    // FRAME_SPAN frames sound at once.
    this.slots = [];
    for (let k = 0; k < FRAME_SPAN; k++) this.slots.push(newSlot());
    // twin._ladder_static: one ladder over the whole note when nothing modulates the cutoff
    this.stat = newSlot();
    this.lfoVals = [];
    this.key = null;
    this.startedAt = 0;
    this.releasedAt = 0;
    this.mode = null;
  }

  /** Begin (or retrigger) a note, continuous from the current level (no click). */
  start(note, { detune = 0, gain = 1, glideFrom = null } = {}) {
    const wasActive = this.active;
    const level = wasActive ? this.vcaLevel() : 0.0;
    if (wasActive) {
      // frames of the old note stop taking input and ring out their last two hops
      for (const sl of this.slots) {
        if (sl.on) {
          sl.inputOff = true;
          sl.end = 2 * this.e.hop;
        }
      }
      const st = this.stat;
      if (st.on && (st.inputOff || st.fall !== Infinity)) {
        // a static ladder already on its way out keeps ringing, input off
        st.inputOff = true;
        st.end = 2 * this.e.hop;
      }
    } else {
      for (const sl of this.slots) sl.on = false;
      this.stat.on = false;
    }
    this.active = true;
    this.released = false;
    this.target = note;
    this.pitch = glideFrom === null ? note : glideFrom;
    this.detune = detune;
    this.gain = gain;
    this.i = 0; // samples since note-on
    this.T = Infinity; // note-off time, seconds since note-on
    this.om = 1.0 - level; // 1 - attack factor
    this.x = 1.0; // decay factor
    this.r = 1.0; // release factor
    this.lfoPh = 0.0; // twin: the LFO restarts at phase 0 each note
    this.lfoVals.length = 0;
    this.lfoRng = null;
    if (!wasActive) {
      // twin: phase starts at 0 each note; a retrigger keeps it (a phase jump would click)
      this.p = 0.0;
      this.q = 0.0;
    }
    this.noiseIdx = 0; // twin: every note draws default_rng(seed) from the top
    this.ctl = 0;
    this.version = -1;
    this.dirty = false;
    this.fresh = true;
  }

  /** Note-off now, or at atSec seconds after note-on (exact scheduling for offline renders). */
  release(atSec) {
    if (!this.active) return;
    this.T = atSec === undefined ? this.i / this.e.sr : Math.max(0.0, atSec);
    this.released = true;
    this.releasedAt = this.e.clock;
    this.dirty = true;
  }

  vcaLevel() {
    const P = this.P || this.e.P;
    const att = 1.0 - this.om;
    if (P.gateMode) return att * this.r;
    return att * (P.S + (1.0 - P.S) * this.x) * this.r;
  }

  _refresh() {
    const P = this.e.P;
    const sr = this.e.sr;
    this.P = P;
    this.version = this.e.version;
    this.fa = Math.exp(-3.0 / (P.A * sr));
    this.fx = Math.exp(-1.0 / (P.D * sr));
    this.fr = Math.exp(-1.0 / (P.R * sr));
    this.lfoSteps = Math.max(2, pyRound((P.lfoRate * this.e.nNominal) / sr) + 1);
    this.lfoTab = this.e.lfo[P.lfoShape === 'inv_saw' ? 'saw' : P.lfoShape] || null;
    this.lfoSign = P.lfoShape === 'inv_saw' ? -1.0 : 1.0;
    this.subMul = 2.0 ** P.subShift;
    this.logCut = Math.log2(Math.max(P.cutoff, 1e-3));
    this.dirty = true;
  }

  // ---- the model at a later time, continued from the current state (knobs held) ----

  /** Full ADSR env (what drives the filter) dt seconds after the current sample. */
  _envAt(dt) {
    const P = this.P;
    const t = this.i / this.e.sr, t2 = t + dt, T = this.T;
    const held = Math.min(t, T), held2 = Math.min(t2, T);
    const om = this.om * Math.exp((-3.0 * (held2 - held)) / P.A);
    const x = this.x * Math.exp(-(Math.max(held2, P.A) - Math.max(held, P.A)) / P.D);
    const r = T === Infinity ? this.r : this.r * Math.exp(-(Math.max(t2, T) - Math.max(t, T)) / P.R);
    return (1.0 - om) * (P.S + (1.0 - P.S) * x) * r;
  }

  /** LFO (depth applied) at sample index j since note-on. */
  _lfoAt(j) {
    const P = this.P;
    if (P.lfoShape === 'noise') {
      const idx = Math.floor((j * (this.lfoSteps - 1)) / Math.max(1, this.e.nNominal));
      if (!this.lfoRng) this.lfoRng = new NumpyRng(1234);
      while (this.lfoVals.length <= idx) this.lfoVals.push(this.lfoRng.uniform(-1.0, 1.0));
      return P.lfoDepth * this.lfoVals[idx];
    }
    let ph = this.lfoPh + (P.lfoRate * (j - this.i)) / this.e.sr;
    ph -= Math.floor(ph);
    const tab = this.lfoTab;
    const xi = ph * LFO_N;
    const ii = xi | 0;
    return P.lfoDepth * this.lfoSign * (tab[ii] + (xi - ii) * (tab[ii + 1] - tab[ii]));
  }

  _pitchAt(dt) {
    if (this.pitch === this.target) return this.pitch;
    return this.target + (this.pitch - this.target) * Math.exp(-dt / this.P.glideTau);
  }

  /** twin.render's cutoff before framing: log2 space, key follow, env + LFO, soft bounds. */
  _rawCut(env, lfo, pitch) {
    const P = this.P;
    let x = this.logCut + (P.keyFollow * (pitch - 60)) / 12.0 + P.envAmt * env + P.lfoCut * lfo;
    const hi = this.e.hiLog2, lo = this.e.loLog2;
    x = hi - softplus(hi - x);
    x = lo + softplus(x - lo);
    return 2.0 ** x;
  }

  /**
   * twin._ladder_tv's frame cutoff: the MEAN (in Hz) of the raw cutoff over the frame's
   * two hops [(f-1)hop, (f+1)hop), by the midpoint rule on FM_STEP-sample blocks. Future
   * points come from the continuation; times before note-on take the value at t=0 (twin
   * pads that way). A frame re-aimed mid-window (a knob moved) keeps its old mean `prev`
   * for the part already played, weighted by that part's length.
   */
  _frameMean(f, prev) {
    const e = this.e, hop = e.hop;
    const a = (f - 1) * hop, b = (f + 1) * hop;
    const now = this.i;
    let sum = 0.0, wsum = 0.0;
    if (prev !== undefined && now > a) {
      const w = Math.min(now, b) - a;
      sum += prev * w;
      wsum += w;
    }
    let t0 = prev !== undefined ? Math.max(a, now) : a;
    if (t0 < 0) {
      const w = Math.min(0, b) - t0;
      sum += this.cut0 * w;
      wsum += w;
      t0 = 0;
    }
    while (t0 < b) {
      const t1 = Math.min(b, (Math.floor(t0 / FM_STEP) + 1) * FM_STEP);
      const w = t1 - t0;
      const tm = 0.5 * (t0 + t1 - 1); // mean sample index of [t0, t1)
      const dt = (tm - now) / e.sr;
      sum += w * this._rawCut(this._envAt(dt), this._lfoAt(tm), this._pitchAt(dt));
      wsum += w;
      t0 = t1;
    }
    return wsum > 0 ? sum / wsum : this.cut0;
  }

  _startFrame(f) {
    const hop = this.e.hop;
    let sl = null;
    for (const s of this.slots) if (!s.on) sl = s;
    if (!sl) {
      // all slots ring (a retrigger's ring-outs can crowd them): fold the oldest into the rest
      for (const s of this.slots) if (!sl || s.end < sl.end) sl = s;
      this._handOff(sl);
    }
    sl.on = true;
    sl.inputOff = false;
    sl.frame = f;
    sl.w0 = (f - 1) * hop;
    sl.end = (f - 1 + FRAME_SPAN) * hop;
    sl.x1 = sl.x2 = sl.x3 = sl.x4 = 0.0;
    setSlot(sl, this._frameMean(f), this.P.res, this.e.sr, this.tmp);
  }

  _staticCut() {
    // unmodulated: twin._ladder_static filters the whole note at cutoff_t[0]
    return this._rawCut(0.0, 0.0, this.pitch);
  }

  /** Frame boundary j (sample j*hop): open the next frame, or switch static <-> framed. */
  _frameTick(j) {
    const P = this.P, hop = this.e.hop, st = this.stat;
    const want = P.modulated ? 'tv' : 'static';
    if (j === 0) {
      const sounding = st.on && !st.inputOff; // a held static ladder from the last note
      if (want === 'tv') {
        if (sounding) {
          // retrigger over a sounding static ladder: it plays frame 0's falling half
          st.fall = 0;
          st.end = 3 * hop;
        } else {
          this._startFrame(0);
        }
        this._startFrame(1);
      } else {
        if (!sounding) {
          st.on = true;
          st.x1 = st.x2 = st.x3 = st.x4 = 0.0;
        }
        st.inputOff = false;
        st.rise = -Infinity;
        st.fall = Infinity;
        st.end = Infinity;
        setSlot(st, this._staticCut(), P.res, this.e.sr, this.tmp);
      }
      this.mode = want;
      return;
    }
    const i = j * hop;
    if (this.mode === 'tv' && want === 'tv') {
      this._startFrame(j + 1);
    } else if (this.mode === 'tv' && want === 'static') {
      if (!st.on) st.x1 = st.x2 = st.x3 = st.x4 = 0.0; // a ringing one keeps its state
      st.on = true;
      st.inputOff = false;
      st.rise = i; // takes frame j+1's rising half, then holds
      st.fall = Infinity;
      st.end = Infinity;
      setSlot(st, this._staticCut(), P.res, this.e.sr, this.tmp);
      this.mode = 'static';
    } else if (this.mode === 'static' && want === 'tv') {
      st.fall = i; // plays frame j's falling half, then rings out like a frame
      st.end = i + 3 * hop;
      this._startFrame(j + 1);
      this.mode = 'tv';
    }
  }

  /**
   * A frame's span ends. By then a normal tail is inaudible, but a near-self-oscillating
   * ladder still rings, and cutting it would click. So what is left joins the newest
   * ladder: the output stays continuous (the ladder is linear, y = sum of x4) and the
   * tail keeps decaying under current coefficients.
   */
  _handOff(sl) {
    sl.on = false;
    let to = null;
    if (this.stat.on && !this.stat.inputOff && this.stat !== sl && this.mode === 'static') to = this.stat;
    else {
      for (const s of this.slots) if (s.on && !s.inputOff && s !== sl && (!to || s.w0 > to.w0)) to = s;
    }
    if (!to) return;
    to.x1 += sl.x1;
    to.x2 += sl.x2;
    to.x3 += sl.x3;
    to.x4 += sl.x4;
  }

  /** A knob moved or the key came up: re-aim the frames still taking input. */
  _reaim() {
    const P = this.P, sr = this.e.sr, win = 2 * this.e.hop, i = this.i;
    for (const sl of this.slots) {
      if (sl.on && !sl.inputOff && i < sl.w0 + win) setSlot(sl, this._frameMean(sl.frame, sl.fc), P.res, sr, this.tmp);
    }
    if (this.stat.on) setSlot(this.stat, this._staticCut(), P.res, sr, this.tmp);
    this.dirty = false;
  }

  _control() {
    const e = this.e, P = this.P, sr = e.sr;
    if (this.dirty) this._reaim();
    else if (this.stat.on && this.pitch !== this.target && P.keyFollow !== 0.0) {
      setSlot(this.stat, this._staticCut(), P.res, sr, this.tmp); // key follow tracks the glide
    }
    const f0 = midiToHz(this.pitch);
    const fTop = f0 * 2.0 ** (100.0 / 1200.0);
    this.H = Math.min(harmonicCount(fTop, e.modelSr, e.maxH), harmonicCount(fTop, sr, e.maxH));
    this.Hs = Math.max(1, Math.floor(this.H / 2));
    this.tune = 2.0 ** ((P.cents + this.detune) / 1200.0);
    this.fBase = f0 * this.tune;
  }

  /** Render n samples at offset off, ADDING gain-scaled osc / filter / amp. */
  process(osc, filt, amp, off, n) {
    const e = this.e;
    const sr = e.sr, invSr = 1.0 / sr, hop = e.hop, hann = e.hann, win = 2 * hop;
    if (this.version !== e.version) this._refresh();
    if (this.fresh) {
      this.cut0 = this._rawCut(this._envAt(0.0), this._lfoAt(0), this.pitch);
      this.fresh = false;
    }
    const tables = e.saw, noise = e.noise, noiseLen = noise.length, noiseScale = e.noiseScale;
    const g = this.gain, slots = this.slots, nSlots = slots.length, st = this.stat;
    // hot state lives in locals; helpers read this.*, so it is synced around them
    let i = this.i, om = this.om, x = this.x, r = this.r, p = this.p, q = this.q;
    let lfoPh = this.lfoPh, nIdx = this.noiseIdx, ctl = this.ctl;
    let P = this.P, tab = null, stab = null, lfoTab = null, lfoGain = 0.0, noiseLvl = 0.0;
    let A = 0.0, D = 0.0, S = 0.0, R = 0.0, fa = 0.0, fx = 0.0, fr = 0.0, gate = false;
    let lfoInc = 0.0, pw2 = 0.0, sd2 = 0.0, subInc = 0.0, sawL = 0.0, sqL = 0.0, subL = 0.0;
    let noiseLfo = false, lfoPitch = 0.0, fBase = this.fBase;
    for (let s = 0; s < n; s++) {
      const tick = i % hop === 0;
      if (ctl === 0 || tick || s === 0) {
        this.i = i;
        this.om = om;
        this.x = x;
        this.r = r;
        this.lfoPh = lfoPh;
        if (ctl === 0) {
          if (this.version !== e.version) this._refresh();
          this._control();
        }
        if (tick) this._frameTick(i / hop);
        P = this.P;
        tab = tables[this.H];
        stab = tables[this.Hs];
        lfoTab = this.lfoTab;
        lfoGain = P.lfoDepth * this.lfoSign;
        noiseLfo = P.lfoShape === 'noise';
        lfoInc = P.lfoRate * invSr;
        lfoPitch = P.lfoPitch;
        noiseLvl = P.noise * noiseScale;
        A = P.A;
        D = P.D;
        S = P.S;
        R = P.R;
        fa = this.fa;
        fx = this.fx;
        fr = this.fr;
        gate = P.gateMode;
        pw2 = 0.5 * P.pw;
        sd2 = 0.5 * P.subDuty;
        subInc = this.subMul * invSr;
        sawL = P.saw;
        sqL = P.square;
        subL = P.sub;
        fBase = this.fBase;
      }
      // ---- envelope: the state machine equals twin._adsr's closed form when knobs hold ----
      const att = 1.0 - om;
      const env = att * (S + (1.0 - S) * x) * r; // full ADSR (what drives the filter)
      const vca = gate ? att * r : env;
      // ---- LFO ----
      let lfo;
      if (noiseLfo) {
        this.i = i;
        lfo = this._lfoAt(i);
      } else {
        const xi = lfoPh * LFO_N;
        const ii = xi | 0;
        lfo = lfoGain * (lfoTab[ii] + (xi - ii) * (lfoTab[ii + 1] - lfoTab[ii]));
        lfoPh += lfoInc;
        lfoPh -= Math.floor(lfoPh);
      }
      // ---- pitch: glide, fine tune, LFO vibrato (twin: f0 * 2^(cents/1200 + semis/12)) ----
      if (this.pitch !== this.target) {
        const d = this.target - this.pitch;
        this.pitch = Math.abs(d) < 1e-4 ? this.target : this.pitch + d * (1.0 - Math.exp(-invSr / P.glideTau));
        fBase = midiToHz(this.pitch) * this.tune;
        this.fBase = fBase;
      }
      const f = lfoPitch !== 0.0 ? fBase * Math.exp(((lfoPitch * lfo) / 12.0) * LN2) : fBase;
      // ---- oscillators: advance the phase, then read (twin's cumsum includes this sample) ----
      p += f * invSr;
      p -= Math.floor(p);
      q += f * subInc;
      q -= Math.floor(q);
      let mix = 0.0;
      if (sawL !== 0.0) mix += sawL * readCubic(tab, p);
      if (sqL !== 0.0) {
        let u = p - pw2 + 0.5;
        u -= Math.floor(u);
        let w = p + pw2 + 0.5;
        w -= Math.floor(w);
        mix += sqL * (readCubic(tab, u) - readCubic(tab, w));
      }
      if (subL !== 0.0) {
        let u = q - sd2 + 0.5;
        u -= Math.floor(u);
        let w = q + sd2 + 0.5;
        w -= Math.floor(w);
        mix += subL * (readCubic(stab, u) - readCubic(stab, w));
      }
      if (noiseLvl !== 0.0) mix += noiseLvl * noise[nIdx];
      nIdx = nIdx + 1 === noiseLen ? 0 : nIdx + 1;
      // ---- the ladder(s): static = one ladder; framed = Hann-windowed input per frame ----
      let y = 0.0;
      if (st.on) {
        let wt = 0.0;
        if (!st.inputOff) {
          if (i >= st.fall) wt = i < st.fall + hop ? hann[hop + i - st.fall] : 0.0;
          else if (i < st.rise + hop) wt = i < st.rise ? 0.0 : hann[i - st.rise];
          else wt = 1.0;
        }
        y += stepSlot(st, wt * mix);
        if (i + 1 >= st.end) this._handOff(st);
      }
      for (let k = 0; k < nSlots; k++) {
        const sl = slots[k];
        if (!sl.on) continue;
        const m = i - sl.w0;
        if (sl.inputOff || m >= win) {
          // ringing out: stop once the tail is gone (-240 dB), else at the span's end
          if (sl.x1 * sl.x1 + sl.x2 * sl.x2 + sl.x3 * sl.x3 + sl.x4 * sl.x4 < 1e-24) {
            sl.on = false;
            continue;
          }
          y += stepSlot(sl, 0.0);
        } else {
          y += stepSlot(sl, m >= 0 ? hann[m] * mix : 0.0);
        }
        if (i + 1 >= sl.end) this._handOff(sl);
      }
      const j = off + s;
      osc[j] += g * mix;
      filt[j] += g * y;
      amp[j] += g * (y * vca);
      // ---- advance the envelope one sample ----
      const T = this.T, t = i * invSr, t2 = (i + 1) * invSr;
      if (t2 <= T) {
        om *= fa;
        if (t >= A) x *= fx;
        else if (t2 > A) x *= Math.exp(-(t2 - A) / D);
      } else if (t < T) {
        om *= Math.exp((-3.0 * (T - t)) / A);
        if (T > A) x *= Math.exp(-(T - Math.max(t, A)) / D);
        r *= Math.exp(-(t2 - T) / R);
      } else {
        r *= fr;
      }
      i++;
      ctl = ctl + 1 === CONTROL ? 0 : ctl + 1;
    }
    this.i = i;
    this.om = om;
    this.x = x;
    this.r = r;
    this.p = p;
    this.q = q;
    this.lfoPh = lfoPh;
    this.noiseIdx = nIdx;
    this.ctl = ctl;
    // A released voice stops at -180 dB: far below hearing, and below the log-mel
    // floor (1e-8 power) the parity gate compares with, so stopping is not a difference.
    if (this.released && i / sr > this.T && this.vcaLevel() < 1e-9) this.active = false;
  }
}

/**
 * The polyphonic twin: maxVoices Voices (the S-1's 4 by default; a page may ask for
 * 8 or 16, VOICE_COUNTS) plus the S-1's voice modes (CC80 Mono / Unison / Poly /
 * Chord, chord voices from CC81-83 + CC85-87, glide from CC31 / CC65 / CC5, Range
 * CC14, damper CC64). Only Poly uses voices past the fourth: Unison stacks 4, Chord
 * plays the key + 3. The voice layer is browser behavior around the model — twin.py
 * renders one note at a time.
 */
export class Engine {
  constructor({ sr, curves, modelSr, maxVoices = 4, noiseSeed = 0 }) {
    requireCurves(curves);
    this.sr = sr;
    this.curves = curves;
    this.modelSr = modelSr ?? curves.sr;
    this.maxH = curves.max_harmonics ?? 64;
    this.saw = sawTables(this.maxH);
    this.lfo = lfoTables();
    this.noise = noiseTable(noiseSeed);
    // twin.py noise is N(0,1) per sample at its own rate; keep the same density per Hz.
    this.noiseScale = Math.sqrt(sr / this.modelSr);
    const blockModel = Math.max(8, Math.floor(this.modelSr / 50));
    this.hop = Math.max(1, Math.round((blockModel * sr) / this.modelSr)); // twin._ladder_tv hop at sr
    this.hann = hanning(2 * this.hop); // twin._ladder_tv's analysis window (numpy.hanning)
    this.hiLog2 = Math.log2(0.45 * this.modelSr);
    this.loLog2 = Math.log2(20.0);
    this.nNominal = Math.max(1, pyRound((curves.seconds ?? 2.0) * sr)); // noise-LFO step grid
    this.cc = new Map();
    for (const [k, r] of Object.entries(curves.cc_ranges || {})) this.cc.set(Number(k), r[2]);
    this.voices = [];
    for (let v = 0; v < maxVoices; v++) this.voices.push(new Voice(this, v));
    this.version = 0;
    this.P = voiceParams(this.cc, curves);
    this.held = []; // keys down, in order (mono / unison / chord note stack)
    this.sustained = new Set();
    this.lastPitch = null;
    this.clock = 0;
  }

  /** Set one CC (0..127 space). Takes effect at the next process() block. */
  set(cc, v) {
    cc = Number(cc);
    v = Number(v);
    if (!Number.isFinite(cc) || !Number.isFinite(v)) return;
    const prevPoly = this.P.poly;
    this.cc.set(cc, v);
    this.P = voiceParams(this.cc, this.curves);
    this.version++;
    if (cc === 80 && this.P.poly !== prevPoly) this.allOff();
    if (cc === 64 && !this.P.damper) {
      for (const note of [...this.sustained]) if (!this.held.includes(note)) this._releaseKey(note);
      this.sustained.clear();
    }
  }

  setAll(map) {
    const entries = map instanceof Map ? [...map.entries()] : Object.entries(map || {});
    for (const [cc, v] of entries) this.cc.set(Number(cc), Number(v));
    const prevPoly = this.P.poly;
    this.P = voiceParams(this.cc, this.curves);
    this.version++;
    if (this.P.poly !== prevPoly) this.allOff();
  }

  /** The voices one key press sounds in the current mode: [{note, detune, gain}]. */
  voicesFor(note) {
    const P = this.P;
    const base = note + P.rangeShift;
    if (P.poly === 1) return UNISON_DETUNE.map((d) => ({ note: base, detune: d, gain: 0.5 }));
    if (P.poly === 3) {
      const list = [{ note: base, detune: 0, gain: 1 }];
      for (let v = 0; v < 3; v++) {
        if (P.chordOn[v]) list.push({ note: base + P.chordShift[v], detune: 0, gain: 1 });
      }
      return list.slice(0, this.voices.length);
    }
    return [{ note: base, detune: 0, gain: 1 }];
  }

  _glideFrom(legato) {
    const mode = this.P.glide;
    if (this.lastPitch === null || mode === 0) return null;
    if (mode === 1 && !legato) return null;
    return this.lastPitch;
  }

  _pickVoice(note) {
    const vs = this.voices;
    for (const v of vs) if (v.active && v.key === note) return v;
    for (const v of vs) if (!v.active) return v;
    let best = null;
    for (const v of vs) if (v.released && (!best || v.releasedAt < best.releasedAt)) best = v;
    if (best) return best;
    for (const v of vs) if (!best || v.startedAt < best.startedAt) best = v;
    return best;
  }

  noteOn(note, vel = 100) {
    note = Math.round(Number(note));
    if (!Number.isFinite(note)) return;
    if (vel <= 0) return this.noteOff(note);
    this.clock++;
    const legato = this.held.length > 0;
    this.held = this.held.filter((n) => n !== note);
    this.held.push(note);
    this.sustained.delete(note);
    const specs = this.voicesFor(note);
    const glideFrom = this._glideFrom(legato);
    if (this.P.poly === 2) {
      const v = this._pickVoice(note);
      v.start(specs[0].note, { detune: specs[0].detune, gain: specs[0].gain, glideFrom });
      v.key = note;
      v.startedAt = this.clock;
    } else {
      // Mono / Unison / Chord: one key at a time drives the voice group
      specs.forEach((sp, idx) => {
        const v = this.voices[idx];
        const from = glideFrom === null ? null : glideFrom + (sp.note - specs[0].note);
        v.start(sp.note, { detune: sp.detune, gain: sp.gain, glideFrom: from });
        v.key = note;
        v.startedAt = this.clock;
      });
      for (let idx = specs.length; idx < this.voices.length; idx++) {
        if (this.voices[idx].active && !this.voices[idx].released) this.voices[idx].release();
      }
    }
    this.lastPitch = specs[0].note;
  }

  /** Key up. atSec (seconds since that voice's note-on) schedules it exactly — offline use. */
  noteOff(note, atSec) {
    note = Math.round(Number(note));
    const wasTop = this.held.length && this.held[this.held.length - 1] === note;
    this.held = this.held.filter((n) => n !== note);
    if (this.P.damper && atSec === undefined) {
      this.sustained.add(note);
      return;
    }
    if (this.P.poly !== 2 && wasTop && this.held.length) {
      // last-note priority: fall back to the key still held, legato (no retrigger)
      const back = this.held[this.held.length - 1];
      const specs = this.voicesFor(back);
      const glide = this.P.glide !== 0;
      specs.forEach((sp, idx) => {
        const v = this.voices[idx];
        if (!v.active) return;
        v.key = back;
        v.target = sp.note;
        if (!glide) v.pitch = sp.note;
        v.dirty = true;
      });
      this.lastPitch = specs[0].note;
      return;
    }
    this._releaseKey(note, atSec);
  }

  _releaseKey(note, atSec) {
    for (const v of this.voices) if (v.active && v.key === note && !v.released) v.release(atSec);
  }

  allOff() {
    this.held = [];
    this.sustained.clear();
    for (const v of this.voices) if (v.active && !v.released) v.release();
  }

  /** Hard stop (no release tail). */
  panic() {
    this.allOff();
    for (const v of this.voices) v.active = false;
  }

  get activeVoices() {
    return this.voices.filter((v) => v.active).length;
  }

  /** Render n samples into osc / filt / amp (overwritten). */
  process(osc, filt, amp, n = osc.length) {
    osc.fill(0, 0, n);
    filt.fill(0, 0, n);
    amp.fill(0, 0, n);
    for (const v of this.voices) if (v.active) v.process(osc, filt, amp, 0, n);
  }
}
