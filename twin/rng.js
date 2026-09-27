// rng.js — numpy's default_rng, reproduced so the browser twin's noise IS twin.py's noise.
//
// twin.py draws its oscillator noise from np.random.default_rng(0).standard_normal(n)
// and its "noise" LFO from default_rng(1234).uniform(-1, 1). A different white-noise
// realization would still be "white noise", but a noise-heavy patch would then miss
// the parity gate by the log-mel spread of two independent noises. So this module
// ports numpy's generator bit for bit:
//
//   * SeedSequence (entropy pool + hash)   <- numpy/random/bit_generator.pyx
//   * PCG64 (128-bit LCG, XSL-RR output)   <- numpy/random/src/pcg64/pcg64.h
//   * float64 uniform = (u64 >> 11) / 2^53 <- numpy/random/src/distributions
//   * standard_normal: 256-layer ziggurat  <- distributions.c random_standard_normal
//
// Checked against numpy 2.x: PCG64 raw output and uniform() are exact. The ziggurat
// tables are rebuilt from two constants below; the result matches numpy's compiled
// tables to ~1e-14 relative, so standard_normal agrees to ~2e-15 absolute with no
// stream desync (tests/test_twin_parity.py renders a noise patch through both).
//
// 128-bit arithmetic runs on 16-bit limbs held in plain numbers: every partial sum
// stays below 2^53, so it is exact without BigInt (which is too slow per sample).

const M16 = 65536;
// PCG_DEFAULT_MULTIPLIER_128 = 0x2360ED051FC65DA4_4385DF649FCCF645, 16-bit limbs, LSB first.
const MULT = [0xf645, 0x9fcc, 0xdf64, 0x4385, 0x5da4, 0x1fc6, 0xed05, 0x2360];

// SeedSequence constants (bit_generator.pyx).
const INIT_A = 0x43b0d7e5, MULT_A = 0x931e8875;
const INIT_B = 0x8b51f9dd, MULT_B = 0x58f38ded;
const MIX_MULT_L = 0xca01f9dd, MIX_MULT_R = 0x4973f715;

function seedWords(seed) {
  // _int_to_uint32_array: little-endian 32-bit words; 0 -> [0].
  let n = Math.floor(Math.abs(Number(seed) || 0));
  const out = [];
  if (n === 0) out.push(0);
  while (n > 0) {
    out.push(n % 4294967296);
    n = Math.floor(n / 4294967296);
  }
  return out;
}

/** numpy SeedSequence(seed).generate_state(4, uint64), as 8 uint32 words (LSW first). */
function seedState(seed) {
  const ent = seedWords(seed);
  const pool = [0, 0, 0, 0];
  let hc = INIT_A;
  const hashmix = (v) => {
    v = (v ^ hc) >>> 0;
    hc = Math.imul(hc, MULT_A) >>> 0;
    v = Math.imul(v, hc) >>> 0;
    return (v ^ (v >>> 16)) >>> 0;
  };
  const mix = (x, y) => {
    const r = (Math.imul(MIX_MULT_L, x) - Math.imul(MIX_MULT_R, y)) >>> 0;
    return (r ^ (r >>> 16)) >>> 0;
  };
  for (let i = 0; i < 4; i++) pool[i] = hashmix(i < ent.length ? ent[i] : 0);
  for (let s = 0; s < 4; s++) {
    for (let d = 0; d < 4; d++) if (s !== d) pool[d] = mix(pool[d], hashmix(pool[s]));
  }
  for (let s = 4; s < ent.length; s++) {
    for (let d = 0; d < 4; d++) pool[d] = mix(pool[d], hashmix(ent[s]));
  }
  let hb = INIT_B;
  const words = [];
  for (let i = 0; i < 8; i++) {
    let v = (pool[i % 4] ^ hb) >>> 0;
    hb = Math.imul(hb, MULT_B) >>> 0;
    v = Math.imul(v, hb) >>> 0;
    words.push((v ^ (v >>> 16)) >>> 0);
  }
  return words;
}

// A 128-bit value from two uint64s (hi, lo), each given as [lo32, hi32].
function limbs128(hi, lo) {
  const w = [lo[0], lo[1], hi[0], hi[1]];
  const out = new Array(8);
  for (let i = 0; i < 4; i++) {
    out[2 * i] = w[i] & 0xffff;
    out[2 * i + 1] = w[i] >>> 16;
  }
  return out;
}

// ── ziggurat tables (256 layers, 52-bit), rebuilt the Marsaglia–Tsang way ──
// NOR_R is numpy's ziggurat_nor_r. NOR_V is the layer area; this value (the one
// implied by numpy's own wi_double[0]) makes the rebuilt tables agree with numpy's
// compiled ones to ~1e-14.
const NOR_R = 3.6541528853610088;
const NOR_INV_R = 0.27366123732975827203338247596;
const NOR_V = 0.00492867323397465;
const KI = new Float64Array(256);
const WI = new Float64Array(256);
const FI = new Float64Array(256);
(function buildZiggurat() {
  const m1 = 4503599627370496; // 2^52
  let dn = NOR_R;
  let tn = dn;
  const q = NOR_V / Math.exp(-0.5 * dn * dn);
  KI[0] = Math.trunc((dn / q) * m1);
  KI[1] = 0;
  WI[0] = q / m1;
  WI[255] = dn / m1;
  FI[0] = 1.0;
  FI[255] = Math.exp(-0.5 * dn * dn);
  for (let i = 254; i >= 1; i--) {
    dn = Math.sqrt(-2.0 * Math.log(NOR_V / dn + Math.exp(-0.5 * dn * dn)));
    KI[i + 1] = Math.trunc((dn / tn) * m1);
    tn = dn;
    FI[i] = Math.exp(-0.5 * dn * dn);
    WI[i] = dn / m1;
  }
})();

/** numpy's np.random.default_rng(seed): the PCG64 stream and two of its samplers. */
export class NumpyRng {
  constructor(seed = 0) {
    const w = seedState(seed);
    // generate_state(4, uint64): u64[j] = w[2j] | w[2j+1] << 32
    const u = [[w[0], w[1]], [w[2], w[3]], [w[4], w[5]], [w[6], w[7]]];
    const initstate = limbs128(u[0], u[1]);
    const initseq = limbs128(u[2], u[3]);
    // inc = (initseq << 1) | 1  (mod 2^128)
    this.inc = new Float64Array(8);
    let carry = 0;
    for (let i = 0; i < 8; i++) {
      const v = initseq[i] * 2 + carry;
      this.inc[i] = v % M16;
      carry = v >= M16 ? 1 : 0;
    }
    this.inc[0] |= 1;
    this.s = new Float64Array(8);
    this._step();
    carry = 0;
    for (let i = 0; i < 8; i++) {
      const v = this.s[i] + initstate[i] + carry;
      this.s[i] = v % M16;
      carry = v >= M16 ? 1 : 0;
    }
    this._step();
    this.hi = 0; // last output, high / low 32 bits
    this.lo = 0;
  }

  _step() {
    // s = s * MULT + inc (mod 2^128), unrolled: 36 partial products, each sum < 2^40.
    const s = this.s, inc = this.inc;
    const s0 = s[0], s1 = s[1], s2 = s[2], s3 = s[3], s4 = s[4], s5 = s[5], s6 = s[6], s7 = s[7];
    let acc, n, c = 0;
    acc = c + inc[0] + s0 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[0] = n;
    acc = c + inc[1] + s0 * 40908 + s1 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[1] = n;
    acc = c + inc[2] + s0 * 57188 + s1 * 40908 + s2 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[2] = n;
    acc = c + inc[3] + s0 * 17285 + s1 * 57188 + s2 * 40908 + s3 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[3] = n;
    acc = c + inc[4] + s0 * 23972 + s1 * 17285 + s2 * 57188 + s3 * 40908 + s4 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[4] = n;
    acc = c + inc[5] + s0 * 8134 + s1 * 23972 + s2 * 17285 + s3 * 57188 + s4 * 40908 + s5 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[5] = n;
    acc = c + inc[6] + s0 * 60677 + s1 * 8134 + s2 * 23972 + s3 * 17285 + s4 * 57188 + s5 * 40908 + s6 * 63045;
    c = Math.floor(acc * 1.52587890625e-5);
    n = acc - c * 65536;
    s[6] = n;
    acc = c + inc[7] + s0 * 9056 + s1 * 60677 + s2 * 8134 + s3 * 23972 + s4 * 17285 + s5 * 57188 + s6 * 40908 + s7 * 63045;
    s[7] = acc - Math.floor(acc * 1.52587890625e-5) * 65536;
  }


  /** Advance and leave the 64-bit output in (this.hi, this.lo). */
  next64() {
    this._step();
    const s = this.s;
    // XSL-RR: rotr64(hi64 ^ lo64, state >> 122)
    const xhi = (((s[7] << 16) | s[6]) ^ ((s[3] << 16) | s[2])) >>> 0;
    const xlo = (((s[5] << 16) | s[4]) ^ ((s[1] << 16) | s[0])) >>> 0;
    let rot = s[7] >>> 10;
    let h = xhi, l = xlo;
    if (rot >= 32) {
      h = xlo;
      l = xhi;
      rot -= 32;
    }
    if (rot === 0) {
      this.hi = h;
      this.lo = l;
    } else {
      this.hi = ((h >>> rot) | (l << (32 - rot))) >>> 0;
      this.lo = ((l >>> rot) | (h << (32 - rot))) >>> 0;
    }
  }

  /** numpy next_double: (u64 >> 11) * 2^-53, in [0, 1). */
  random() {
    this.next64();
    return (this.hi * 2097152 + (this.lo >>> 11)) * 1.1102230246251565e-16;
  }

  /** Generator.uniform(low, high) for one sample. */
  uniform(low = 0.0, high = 1.0) {
    return low + (high - low) * this.random();
  }

  /** Generator.standard_normal() for one float64 (ziggurat, numpy's exact control flow). */
  normal() {
    for (;;) {
      this.next64();
      const lo = this.lo;
      const idx = lo & 0xff;
      const sign = (lo >>> 8) & 1;
      // rabs = bits 9..60 of the 64-bit draw (52 bits), exact in a double
      const rabs = (this.hi & 0x1fffffff) * 8388608 + (lo >>> 9);
      let x = rabs * WI[idx];
      if (sign) x = -x;
      if (rabs < KI[idx]) return x; // ~99.3% of draws
      if (idx === 0) {
        const tailSign = (lo >>> 17) & 1; // (rabs >> 8) & 1
        for (;;) {
          const xx = -NOR_INV_R * Math.log1p(-this.random());
          const yy = -Math.log1p(-this.random());
          if (yy + yy > xx * xx) return tailSign ? -(NOR_R + xx) : NOR_R + xx;
        }
      }
      if ((FI[idx - 1] - FI[idx]) * this.random() + FI[idx] < Math.exp(-0.5 * x * x)) return x;
    }
  }

  /** Fill a Float64Array with standard normals (like standard_normal(n)). */
  normals(n) {
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) out[i] = this.normal();
    return out;
  }
}

/** np.random.default_rng(seed).standard_normal(n) as a Float64Array. */
export function standardNormal(seed, n) {
  return new NumpyRng(seed).normals(n);
}

/** np.random.default_rng(seed).uniform(low, high, size=n) as a Float64Array. */
export function uniformArray(seed, low, high, n) {
  const r = new NumpyRng(seed);
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) out[i] = r.uniform(low, high);
  return out;
}
