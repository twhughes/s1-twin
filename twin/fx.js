// fx.js — the browser-only extras AFTER the twin: chorus (CC93), delay (CC92 level,
// CC90 time), reverb (CC91 level, CC89 time), and the output limiter.
//
// twin.py models none of this, so twin.modeled() says false for these CCs. The S-1
// has them, so the page plays them. Every curve below is an uncalibrated guess at
// the hardware. Pure and deterministic: the AudioWorklet runs this live, and
// renderStages() runs the same code offline for its fx / out stages.

/** CCs the effects read (defaults = s1.json factory init). */
export const FX_DEFAULTS = { 89: 100, 90: 87, 91: 0, 92: 0, 93: 0 };

// Chorus types 1-4: LFO rate (Hz), sweep depth and centre delay (s). Guesses.
const CHORUS = [
  null,
  { rate: 0.5, depth: 0.0015, base: 0.008 },
  { rate: 0.9, depth: 0.0025, base: 0.01 },
  { rate: 3.0, depth: 0.0008, base: 0.006 },
  { rate: 0.25, depth: 0.004, base: 0.015 },
];
// Freeverb's comb / all-pass lengths at 44.1 kHz (Jezar's public-domain design).
const COMBS = [1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617];
const ALLPASS = [556, 441, 341, 225];

function clampCC(v) {
  return Math.min(127, Math.max(0, Number(v) || 0));
}

export class Fx {
  constructor(sr) {
    this.sr = sr;
    // chorus: a 60 ms line, two taps a quarter-cycle apart
    this.chN = Math.ceil(0.06 * sr) + 4;
    this.ch = new Float64Array(this.chN);
    this.chW = 0;
    this.chPh = 0;
    // delay: up to 1.25 s, feedback through a gentle lowpass (repeats darken)
    this.dlN = Math.ceil(1.25 * sr) + 4;
    this.dl = new Float64Array(this.dlN);
    this.dlW = 0;
    this.dlLp = 0;
    this.dlCur = 0.3 * sr; // smoothed delay time, in samples (no zipper when CC90 moves)
    this.dlA = Math.exp(-2 * Math.PI * 3000 / sr);
    // reverb: 8 damped combs in parallel, then 4 all-passes
    const scale = sr / 44100;
    this.combs = COMBS.map((n) => ({ buf: new Float64Array(Math.max(1, Math.round(n * scale))), i: 0, store: 0, g: 0 }));
    this.aps = ALLPASS.map((n) => ({ buf: new Float64Array(Math.max(1, Math.round(n * scale))), i: 0 }));
    this.cc = { ...FX_DEFAULTS };
    this._update();
  }

  /** Set one effects CC (others are ignored). */
  set(cc, v) {
    const k = Number(cc);
    if (!(k in FX_DEFAULTS)) return;
    this.cc[k] = clampCC(v);
    this._update();
  }

  _update() {
    const c = this.cc, sr = this.sr;
    this.chorus = CHORUS[Math.round(c[93])] || null;
    this.dlWet = 0.8 * (c[92] / 127);
    this.dlTarget = Math.min(this.dlN - 4, 0.02 * 50 ** (c[90] / 127) * sr); // 20 ms .. 1 s
    this.rvWet = 0.6 * (c[91] / 127);
    const rt60 = 0.4 * 20 ** (c[89] / 127); // 0.4 .. 8 s
    for (const cb of this.combs) cb.g = 10 ** ((-3 * cb.buf.length) / (rt60 * sr));
  }

  /** True when any effect is audible (else fx == amp). */
  get active() {
    return !!this.chorus || this.dlWet > 0 || this.rvWet > 0;
  }

  /** Clear every line (a fresh render starts silent). */
  reset() {
    this.ch.fill(0);
    this.dl.fill(0);
    this.dlLp = 0;
    this.chPh = 0;
    this.dlCur = this.dlTarget;
    for (const cb of this.combs) {
      cb.buf.fill(0);
      cb.store = 0;
    }
    for (const ap of this.aps) ap.buf.fill(0);
  }

  /** out[i] = effects(inp[i]) for i < n (out may be inp). */
  process(inp, out, n) {
    const sr = this.sr;
    const ch = this.ch, chN = this.chN, cho = this.chorus;
    const dl = this.dl, dlN = this.dlN, dlWet = this.dlWet, rvWet = this.rvWet;
    const combs = this.combs, aps = this.aps;
    for (let i = 0; i < n; i++) {
      const x = inp[i];
      // -- chorus (in series) --
      let c = x;
      ch[this.chW] = x;
      if (cho) {
        this.chPh += cho.rate / sr;
        if (this.chPh >= 1) this.chPh -= 1;
        let wet = 0;
        for (let t = 0; t < 2; t++) {
          const d = (cho.base + cho.depth * Math.sin(2 * Math.PI * (this.chPh + 0.25 * t))) * sr;
          let rp = this.chW - d;
          while (rp < 0) rp += chN;
          const i0 = rp | 0;
          const fr = rp - i0;
          wet += ch[i0] + fr * (ch[(i0 + 1) % chN] - ch[i0]);
        }
        c = 0.6 * x + 0.2 * wet;
      }
      this.chW = this.chW + 1 === chN ? 0 : this.chW + 1;
      let y = c;
      // -- delay (send) --
      if (dlWet > 0 || this.dlLp !== 0) {
        this.dlCur += (this.dlTarget - this.dlCur) * 0.0005;
        let rp = this.dlW - this.dlCur;
        while (rp < 0) rp += dlN;
        const i0 = rp | 0;
        const fr = rp - i0;
        const tap = dl[i0] + fr * (dl[(i0 + 1) % dlN] - dl[i0]);
        this.dlLp = tap + this.dlA * (this.dlLp - tap);
        dl[this.dlW] = c + 0.4 * this.dlLp;
        y += dlWet * tap;
      } else {
        dl[this.dlW] = c;
      }
      this.dlW = this.dlW + 1 === dlN ? 0 : this.dlW + 1;
      // -- reverb (send) --
      if (rvWet > 0) {
        const inR = 0.03 * c;
        let acc = 0;
        for (let k = 0; k < 8; k++) {
          const cb = combs[k];
          const o = cb.buf[cb.i];
          cb.store = o * 0.7 + cb.store * 0.3; // damping
          cb.buf[cb.i] = inR + cb.store * cb.g;
          cb.i = cb.i + 1 === cb.buf.length ? 0 : cb.i + 1;
          acc += o;
        }
        for (let k = 0; k < 4; k++) {
          const ap = aps[k];
          const b = ap.buf[ap.i];
          ap.buf[ap.i] = acc + 0.5 * b;
          ap.i = ap.i + 1 === ap.buf.length ? 0 : ap.i + 1;
          acc = b - acc;
        }
        y += rvWet * acc;
      }
      out[i] = y;
    }
  }
}

/** The output stage: a fixed gain, then a soft knee above 0.7 that never passes 1.0. */
export const MASTER_GAIN = 0.5;
export function master(inp, out, n) {
  for (let i = 0; i < n; i++) {
    const x = MASTER_GAIN * inp[i];
    const a = Math.abs(x);
    out[i] = a <= 0.7 ? x : Math.sign(x) * (0.7 + 0.3 * Math.tanh((a - 0.7) / 0.3));
  }
}
