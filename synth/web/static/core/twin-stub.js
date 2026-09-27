// core/twin-stub.js — a stand-in for twin/audio.js (W-twin's browser twin) with the same API
// (docs/design/BUILD.md §2.3), so the plate works before the real twin lands. It is NOT the twin:
// renderStages() runs the small drawing model from docs/design/menura-comp.html, and the live
// voice is plain Web Audio (oscillators, two biquad lowpasses, a gain envelope, delay, reverb).
// The shell loads twin/audio.js whenever it exists and falls back to this file only when it does not.

/** What synth/match/twin.py models (its K_PARAMS and S_PARAMS). The plate dims the rest in twin mode. */
export const MODELED = new Set([20, 19, 21, 23, 15, 74, 71, 24, 25, 26, 73, 75, 30, 72, 3, 13, 17, 76, 22, 12, 28]);

const DEFAULTS = {
  3: 60, 12: 2, 79: 0, 13: 0, 14: 2, 15: 0, 16: 2, 19: 127, 20: 0, 21: 0, 22: 2, 23: 0, 78: 0, 76: 64,
  74: 127, 71: 0, 24: 0, 25: 0, 26: 0, 28: 1, 73: 0, 75: 42, 30: 25, 72: 21, 29: 2,
  92: 0, 90: 87, 91: 0, 89: 100, 17: 15,
};
const noteHz = (n) => 440 * 2 ** ((n - 69) / 12);
const secs = (v) => 0.002 * 2500 ** (v / 127);            // 2 ms .. 5 s
const cutoffHz = (v) => 20 * 1000 ** (v / 127);            // 20 Hz .. 20 kHz
const rangeRatio = (v) => 2 ** ((v ?? 3) - 3);             // 8' = concert pitch

function mulberry32(a) {
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** PolyBLEP: the band-limiting correction for a step at phase t (cycles), phase step dt. */
function blep(t, dt) {
  if (t < dt) { t /= dt; return t + t - t * t - 1; }
  if (t > 1 - dt) { t = (t - 1) / dt; return t * t + t + t + 1; }
  return 0;
}

/** The LFO's value in [-1, 1] at phase p (cycles), per CC12. */
function lfoAt(shape, p, rnd) {
  const f = p - Math.floor(p);
  switch (shape) {
    case 0: return 2 * f - 1;                               // saw
    case 1: return 1 - 2 * f;                               // inverse saw
    case 3: return f < 0.5 ? 1 : -1;                        // square
    case 4: return rnd(Math.floor(p));                      // random steps
    case 5: return rnd(Math.floor(p * 16));                 // noise
    default: return 1 - 4 * Math.abs(f - 0.5);             // triangle
  }
}

/** Offline render of the chain, one Float32Array per stage (the plate's wells draw these). */
export function renderModel(P, { note = 45, seconds = 2.8, gate = 0.62, sr = 22050 } = {}) {
  const n = Math.max(1, Math.round(seconds * sr));
  const osc = new Float32Array(n), filter = new Float32Array(n), amp = new Float32Array(n), fx = new Float32Array(n);
  const f0 = noteHz(note) * rangeRatio(P[14]) * 2 ** (((P[76] ?? 64) - 64) / 64);
  const Lsq = P[19] / 127, Lsaw = P[20] / 127, Lsub = P[21] / 127, Ln = P[23] / 127;
  const norm = Math.max(1, (Lsq + Lsaw + Lsub + Ln) * 0.85);
  const subDiv = P[22] === 2 ? 2 : 4, subDuty = P[22] === 0 ? 0.25 : 0.5;
  const A = secs(P[73]), D = secs(P[75]), S = P[30] / 127, R = secs(P[72]);
  const lfoHz = 0.08 * 300 ** (P[3] / 127) * (P[79] ? 8 : 1);
  const steps = new Map();
  const rndL = mulberry32(99);
  const stepRnd = (k) => { if (!steps.has(k)) steps.set(k, rndL() * 2 - 1); return steps.get(k); };
  const envAt = (t) => {
    const held = (u) => (u < A ? u / A : S + (1 - S) * Math.exp(-(u - A) / (D / 3.2)));
    return t < gate ? held(t) : held(gate) * Math.exp(-(t - gate) / (R / 3.2));
  };
  const gateAt = (t) => (t < gate ? Math.min(1, t / 0.004) : Math.max(0, 1 - (t - gate) / 0.006));
  const fc0 = cutoffHz(P[74]) * (f0 / 110) ** ((P[26] / 127) * 0.9);
  const k = 3.9 * (P[71] / 127);
  const rnd = mulberry32(7);
  let ph = 0, sph = 0, pink = 0, s1 = 0, s2 = 0, s3 = 0, s4 = 0, dcx = 0, dcy = 0;
  const dcR = 1 - (2 * Math.PI * 8) / sr;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    const lfo = lfoAt(P[12], t * lfoHz, stepRnd);
    const env = envAt(t);
    const dt = (f0 * 2 ** ((P[13] / 127) * 2 * lfo / 12)) / sr;
    ph += dt; sph += dt / subDiv;
    const p1 = ph - Math.floor(ph), q1 = sph - Math.floor(sph);
    let pw = 0.5 - 0.46 * (P[15] / 127);
    if (P[16] === 2) pw = 0.5 - 0.46 * (P[15] / 127) * (0.5 + 0.5 * lfo);
    if (P[16] === 0) pw = 0.5 - 0.46 * (P[15] / 127) * env;
    const sq = (p1 < pw ? 1 : -1) + blep(p1, dt) - blep((p1 - pw + 1) % 1, dt);
    const saw = 2 * p1 - 1 - blep(p1, dt);
    const sub = (q1 < subDuty ? 1 : -1) + blep(q1, dt / subDiv) - blep((q1 - subDuty + 1) % 1, dt / subDiv);
    const white = rnd() * 2 - 1;
    pink = 0.96 * pink + 0.04 * white * 3;
    const x = (sq * Lsq + saw * Lsaw + sub * Lsub + (P[78] === 0 ? pink : white) * Ln) / norm;
    dcy = x - dcx + dcR * dcy; dcx = x;                      // the S-1's outputs are AC-coupled
    osc[i] = dcy;
    let fc = fc0 * 2 ** ((P[24] / 127) * 6 * env + (P[25] / 127) * 2.5 * lfo);
    fc = Math.min(fc, sr * 0.45);
    const g = Math.min(0.995, 1 - Math.exp(-2 * Math.PI * fc / sr));
    const u = Math.tanh(dcy - k * s4);
    s1 += g * (u - s1); s2 += g * (s1 - s2); s3 += g * (s2 - s3); s4 += g * (s3 - s4);
    filter[i] = s4 * (1 + k * 0.45);
    amp[i] = filter[i] * (P[28] === 1 ? env : gateAt(t));
  }
  // Effects: an 8-tap feedback delay and four comb filters for the reverb (browser-only extras).
  const DT = Math.round(0.03 * 33 ** (P[90] / 127) * sr), DL = P[92] / 127;
  const RL = P[91] / 127, RT = 0.3 * 20 ** (P[89] / 127);
  const combs = [0.0297, 0.0371, 0.0411, 0.0437].map((d) => {
    const len = Math.max(1, Math.round(d * sr));
    return { buf: new Float32Array(len), i: 0, g: 10 ** (-3 * d / RT) };
  });
  for (let i = 0; i < n; i++) {
    let e = 0;
    for (let r = 1; r <= 8; r++) { const j = i - r * DT; if (j >= 0) e += DL * 0.5 ** (r - 1) * amp[j]; }
    let rv = 0;
    for (const c of combs) { const y = c.buf[c.i]; c.buf[c.i] = amp[i] + y * c.g; c.i = (c.i + 1) % c.buf.length; rv += y; }
    fx[i] = amp[i] + e * 0.85 + rv * RL * 0.3;
  }
  return { sr, osc, filter, amp, fx, out: fx };
}

function impulse(ac, seconds) {
  const len = Math.round(ac.sampleRate * seconds), buf = ac.createBuffer(2, len, ac.sampleRate);
  const rnd = mulberry32(3);
  for (let c = 0; c < 2; c++) {
    const d = buf.getChannelData(c);
    for (let i = 0; i < len; i++) d[i] = (rnd() * 2 - 1) * (1 - i / len) ** 3;
  }
  return buf;
}

export async function createTwin({ curves = null } = {}) {
  const P = { ...DEFAULTS };
  let ac = null, g = null, voice = null;

  function ensure() {
    if (ac) return true;
    const AC = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!AC) return false;
    ac = new AC();
    const an = () => { const a = ac.createAnalyser(); a.fftSize = 2048; a.smoothingTimeConstant = 0; return a; };
    g = {
      bus: ac.createGain(), f1: ac.createBiquadFilter(), f2: ac.createBiquadFilter(), vca: ac.createGain(),
      delay: ac.createDelay(2), fb: ac.createGain(), dWet: ac.createGain(), conv: ac.createConvolver(),
      rWet: ac.createGain(), sum: ac.createGain(), out: ac.createGain(), lfo: ac.createOscillator(),
      lfoPitch: ac.createGain(), lfoCut: ac.createGain(),
      taps: { osc: an(), filter: an(), amp: an(), fx: an(), out: an() },
    };
    g.f1.type = g.f2.type = "lowpass";
    g.vca.gain.value = 0;
    g.out.gain.value = 0.35;
    g.conv.buffer = impulse(ac, 2.2);
    g.bus.connect(g.taps.osc); g.bus.connect(g.f1); g.f1.connect(g.f2); g.f2.connect(g.taps.filter); g.f2.connect(g.vca);
    g.vca.connect(g.taps.amp); g.vca.connect(g.sum);
    g.vca.connect(g.delay); g.delay.connect(g.fb); g.fb.connect(g.delay); g.delay.connect(g.dWet); g.dWet.connect(g.sum);
    g.vca.connect(g.conv); g.conv.connect(g.rWet); g.rWet.connect(g.sum);
    g.sum.connect(g.taps.fx); g.sum.connect(g.out); g.out.connect(g.taps.out); g.out.connect(ac.destination);
    g.lfo.connect(g.lfoPitch); g.lfo.connect(g.lfoCut); g.lfoCut.connect(g.f1.detune); g.lfoCut.connect(g.f2.detune);
    g.lfo.start();
    retune();
    return true;
  }

  const level = (cc) => P[cc] / 127;
  function baseCutoff(note) {
    const f0 = noteHz(note ?? 60);
    return Math.min(cutoffHz(P[74]) * (f0 / 110) ** (level(26) * 0.9), 20000);
  }
  function retune() {
    if (!ac) return;
    const t = ac.currentTime;
    const q = 0.5 + 11 * level(71);
    g.f1.Q.setTargetAtTime(q, t, 0.02); g.f2.Q.setTargetAtTime(q, t, 0.02);
    if (!voice) { g.f1.frequency.setTargetAtTime(baseCutoff(), t, 0.02); g.f2.frequency.setTargetAtTime(baseCutoff(), t, 0.02); }
    g.delay.delayTime.setTargetAtTime(Math.min(1.9, 0.03 * 33 ** level(90)), t, 0.05);
    g.fb.gain.setTargetAtTime(0.45, t, 0.05);
    g.dWet.gain.setTargetAtTime(level(92) * 0.8, t, 0.02);
    g.rWet.gain.setTargetAtTime(level(91) * 0.6, t, 0.02);
    g.lfo.type = ["sawtooth", "sawtooth", "triangle", "square", "square", "square"][P[12]] || "triangle";
    g.lfo.frequency.setTargetAtTime(0.08 * 300 ** level(3) * (P[79] ? 8 : 1), t, 0.02);
    g.lfoPitch.gain.setTargetAtTime(level(13) * 200, t, 0.02);
    g.lfoCut.gain.setTargetAtTime(level(25) * 2.5 * 1200, t, 0.02);
    if (voice) {
      const norm = Math.max(1, (level(19) + level(20) + level(21) + level(23)) * 0.85);
      voice.levels.saw.gain.setTargetAtTime(level(20) / norm, t, 0.02);
      voice.levels.sq.gain.setTargetAtTime(level(19) / norm, t, 0.02);
      voice.levels.sub.gain.setTargetAtTime(level(21) / norm, t, 0.02);
      voice.levels.noise.gain.setTargetAtTime(level(23) * 0.5 / norm, t, 0.02);
    }
  }

  function stopVoice(v, at) {
    for (const s of v.srcs) { try { s.stop(at); } catch { /* already stopped */ } }
  }

  const twin = {
    stub: true,
    set(cc, v) { P[cc] = v; retune(); },
    setAll(map) { for (const [cc, v] of Object.entries(map || {})) P[Number(cc)] = v; retune(); },
    resume() { if (ensure() && ac.state !== "running") ac.resume().catch(() => {}); },
    noteOn(note, vel = 100) {
      if (!ensure()) return;
      const t = ac.currentTime;
      if (voice) stopVoice(voice, t + 0.02);
      const f = noteHz(note) * rangeRatio(P[14]) * 2 ** ((P[76] - 64) / 64);
      const mk = (type, hz) => { const o = ac.createOscillator(); o.type = type; o.frequency.value = hz; g.lfoPitch.connect(o.detune); return o; };
      const saw = mk("sawtooth", f), sq = mk("square", f), sub = mk("square", f / (P[22] === 2 ? 2 : 4));
      const noise = ac.createBufferSource();
      noise.buffer = impulse(ac, 1); noise.loop = true;
      const levels = { saw: ac.createGain(), sq: ac.createGain(), sub: ac.createGain(), noise: ac.createGain() };
      saw.connect(levels.saw); sq.connect(levels.sq); sub.connect(levels.sub); noise.connect(levels.noise);
      Object.values(levels).forEach((l) => l.connect(g.bus));
      voice = { note, srcs: [saw, sq, sub, noise], levels };
      retune();
      [saw, sq, sub, noise].forEach((s) => s.start(t));
      const peak = (vel / 127) * 0.9, A = secs(P[73]), D = secs(P[75]), S = level(30);
      const vca = g.vca.gain;
      vca.cancelScheduledValues(t); vca.setValueAtTime(vca.value, t);
      if (P[28] === 1) { vca.linearRampToValueAtTime(peak, t + A); vca.setTargetAtTime(peak * S, t + A, D / 3.2); }
      else vca.linearRampToValueAtTime(peak, t + 0.004);
      const fc = baseCutoff(note), up = Math.min(fc * 2 ** (level(24) * 6), 20000), rest = Math.min(fc * 2 ** (level(24) * 6 * S), 20000);
      for (const f1 of [g.f1.frequency, g.f2.frequency]) {
        f1.cancelScheduledValues(t); f1.setValueAtTime(fc, t);
        f1.linearRampToValueAtTime(up, t + A); f1.setTargetAtTime(rest, t + A, D / 3.2);
      }
    },
    noteOff(note) {
      if (!ac || !voice || voice.note !== note) return;
      const t = ac.currentTime, R = P[28] === 1 ? secs(P[72]) : 0.006;
      const vca = g.vca.gain;
      vca.cancelScheduledValues(t); vca.setValueAtTime(vca.value, t); vca.setTargetAtTime(0, t, R / 3.2);
      for (const f1 of [g.f1.frequency, g.f2.frequency]) { f1.cancelScheduledValues(t); f1.setTargetAtTime(baseCutoff(note), t, R / 3.2); }
      stopVoice(voice, t + R * 2 + 0.1);
      voice = null;
    },
    allOff() {
      if (!ac || !voice) return;
      const t = ac.currentTime;
      g.vca.gain.cancelScheduledValues(t); g.vca.gain.setTargetAtTime(0, t, 0.01);
      stopVoice(voice, t + 0.1);
      voice = null;
    },
    get taps() { return g ? g.taps : { osc: null, filter: null, amp: null, fx: null, out: null }; },
    async renderStages(opts = {}) { return renderModel(P, opts); },
    modeled: (cc) => MODELED.has(cc),
    level() {
      if (!g) return 0;
      const a = g.taps.out, buf = new Float32Array(a.fftSize);
      a.getFloatTimeDomainData(buf);
      let s = 0; for (const x of buf) s += x * x;
      return Math.sqrt(s / buf.length);
    },
    curves,
  };
  return twin;
}
