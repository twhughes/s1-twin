// fft.js — complex FFT of any length, float64, pure (no DOM / Web Audio).
//
// twin.py filters in the frequency domain with numpy's pocketfft. The offline
// renderer (dsp.renderNote) repeats those exact transforms, and one of them is
// rarely a power of two: the time-varying ladder uses frames of nfft = 4 * block
// with block = sr // 50 (1764 = 2^2 * 3^2 * 7^2 at 22.05 kHz, 3840 at 48 kHz).
// So: radix-2 for powers of two, recursive mixed radix for small prime factors,
// and Bluestein (chirp-z over a power-of-two FFT) when a prime factor is large.
//
// Convention: forward X[k] = sum_j x[j] e^{-2 pi i jk/n}; inverse is unscaled
// (the caller divides by n), like the textbook DFT pair.

const plans = new Map();

/** A cached FFT plan for length n: plan.forward(re, im) / plan.inverse(re, im), in place. */
export function fftPlan(n) {
  let p = plans.get(n);
  if (!p) {
    p = makePlan(n);
    plans.set(n, p);
  }
  return p;
}

function factorize(n) {
  const f = [];
  let m = n;
  for (const p of [4, 2, 3, 5, 7]) {
    while (m % p === 0) {
      f.push(p);
      m /= p;
    }
  }
  for (let p = 11; p * p <= m; p += 2) {
    while (m % p === 0) {
      f.push(p);
      m /= p;
    }
  }
  if (m > 1) f.push(m);
  return f;
}

function makePlan(n) {
  if (n < 1 || !Number.isInteger(n)) throw new Error(`fft: bad length ${n}`);
  if ((n & (n - 1)) === 0) return new Radix2(n);
  const factors = factorize(n);
  if (Math.max(...factors) <= 64) return new MixedRadix(n, factors);
  return new Bluestein(n);
}

class Radix2 {
  constructor(n) {
    this.n = n;
    const half = n >> 1;
    this.cos = new Float64Array(Math.max(1, half));
    this.sin = new Float64Array(Math.max(1, half));
    for (let i = 0; i < half; i++) {
      this.cos[i] = Math.cos((2 * Math.PI * i) / n);
      this.sin[i] = Math.sin((2 * Math.PI * i) / n);
    }
    this.rev = new Uint32Array(n);
    let bits = 0;
    while (1 << bits < n) bits++;
    for (let i = 0; i < n; i++) {
      let r = 0;
      for (let b = 0; b < bits; b++) r |= ((i >>> b) & 1) << (bits - 1 - b);
      this.rev[i] = r;
    }
  }

  forward(re, im) {
    this._run(re, im, 1);
  }

  inverse(re, im) {
    this._run(re, im, -1);
  }

  _run(re, im, dir) {
    const n = this.n, rev = this.rev, cosT = this.cos, sinT = this.sin;
    for (let i = 0; i < n; i++) {
      const j = rev[i];
      if (j > i) {
        let t = re[i];
        re[i] = re[j];
        re[j] = t;
        t = im[i];
        im[i] = im[j];
        im[j] = t;
      }
    }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1;
      const step = n / size;
      for (let start = 0; start < n; start += size) {
        for (let j = start, k = 0; j < start + half; j++, k += step) {
          const l = j + half;
          const c = cosT[k], s = dir * sinT[k];
          // (re + i im) * (c - i s)
          const tr = re[l] * c + im[l] * s;
          const ti = im[l] * c - re[l] * s;
          re[l] = re[j] - tr;
          im[l] = im[j] - ti;
          re[j] += tr;
          im[j] += ti;
        }
      }
    }
  }
}

class MixedRadix {
  constructor(n, factors) {
    this.n = n;
    this.factors = factors;
    this.cos = new Float64Array(n);
    this.sin = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      this.cos[i] = Math.cos((2 * Math.PI * i) / n);
      this.sin[i] = Math.sin((2 * Math.PI * i) / n);
    }
    this.outRe = new Float64Array(n);
    this.outIm = new Float64Array(n);
    const pmax = Math.max(...factors);
    this.tr = new Float64Array(pmax);
    this.ti = new Float64Array(pmax);
  }

  forward(re, im) {
    this._run(re, im, 1);
  }

  inverse(re, im) {
    this._run(re, im, -1);
  }

  _run(re, im, dir) {
    this.dir = dir;
    this._rec(this.n, 0, re, im, 0, 1, 0);
    re.set(this.outRe);
    im.set(this.outIm);
  }

  // DFT of in[inOff + j*stride], j < len, into out[outOff .. outOff+len).
  _rec(len, fi, inRe, inIm, inOff, stride, outOff) {
    const oRe = this.outRe, oIm = this.outIm;
    if (len === 1) {
      oRe[outOff] = inRe[inOff];
      oIm[outOff] = inIm[inOff];
      return;
    }
    const p = this.factors[fi];
    const m = len / p;
    for (let r = 0; r < p; r++) this._rec(m, fi + 1, inRe, inIm, inOff + r * stride, stride * p, outOff + r * m);
    const N = this.n, cosT = this.cos, sinT = this.sin, dir = this.dir;
    const tw = N / len; // W_len^e = W_N^(e * tw)
    const wp = N / p; // W_p^e = W_N^(e * wp)
    const tr = this.tr, ti = this.ti;
    for (let k = 0; k < m; k++) {
      for (let r = 0; r < p; r++) {
        const yr = oRe[outOff + r * m + k], yi = oIm[outOff + r * m + k];
        const e = (r * k * tw) % N;
        const c = cosT[e], s = dir * sinT[e];
        tr[r] = yr * c + yi * s;
        ti[r] = yi * c - yr * s;
      }
      if (p === 2) {
        oRe[outOff + k] = tr[0] + tr[1];
        oIm[outOff + k] = ti[0] + ti[1];
        oRe[outOff + m + k] = tr[0] - tr[1];
        oIm[outOff + m + k] = ti[0] - ti[1];
        continue;
      }
      for (let q = 0; q < p; q++) {
        let sr = 0, si = 0;
        for (let r = 0; r < p; r++) {
          const e = ((r * q) % p) * wp;
          const c = cosT[e], s = dir * sinT[e];
          sr += tr[r] * c + ti[r] * s;
          si += ti[r] * c - tr[r] * s;
        }
        oRe[outOff + q * m + k] = sr;
        oIm[outOff + q * m + k] = si;
      }
    }
  }
}

class Bluestein {
  constructor(n) {
    this.n = n;
    let m = 1;
    while (m < 2 * n - 1) m <<= 1;
    this.m = m;
    this.inner = fftPlan(m);
    // chirp w[k] = e^{-i pi k^2 / n}; k^2 taken mod 2n so the angle stays exact
    this.wr = new Float64Array(n);
    this.wi = new Float64Array(n);
    for (let k = 0; k < n; k++) {
      const a = (Math.PI * ((k * k) % (2 * n))) / n;
      this.wr[k] = Math.cos(a);
      this.wi[k] = -Math.sin(a);
    }
    // B = FFT of conj(w) laid out circularly
    this.bRe = new Float64Array(m);
    this.bIm = new Float64Array(m);
    this.bRe[0] = this.wr[0];
    this.bIm[0] = -this.wi[0];
    for (let k = 1; k < n; k++) {
      this.bRe[k] = this.bRe[m - k] = this.wr[k];
      this.bIm[k] = this.bIm[m - k] = -this.wi[k];
    }
    this.inner.forward(this.bRe, this.bIm);
    this.aRe = new Float64Array(m);
    this.aIm = new Float64Array(m);
  }

  forward(re, im) {
    const { n, m, wr, wi, aRe, aIm, bRe, bIm } = this;
    aRe.fill(0);
    aIm.fill(0);
    for (let k = 0; k < n; k++) {
      aRe[k] = re[k] * wr[k] - im[k] * wi[k];
      aIm[k] = re[k] * wi[k] + im[k] * wr[k];
    }
    this.inner.forward(aRe, aIm);
    for (let k = 0; k < m; k++) {
      const r = aRe[k] * bRe[k] - aIm[k] * bIm[k];
      aIm[k] = aRe[k] * bIm[k] + aIm[k] * bRe[k];
      aRe[k] = r;
    }
    this.inner.inverse(aRe, aIm);
    for (let k = 0; k < n; k++) {
      const cr = aRe[k] / m, ci = aIm[k] / m;
      re[k] = cr * wr[k] - ci * wi[k];
      im[k] = cr * wi[k] + ci * wr[k];
    }
  }

  inverse(re, im) {
    // inverse(x) = conj(forward(conj(x)))
    for (let k = 0; k < this.n; k++) im[k] = -im[k];
    this.forward(re, im);
    for (let k = 0; k < this.n; k++) im[k] = -im[k];
  }
}

/**
 * numpy's irfft(rfft(x, nfft) * H, nfft) for real x (length <= nfft, zero-padded).
 * H is given on the rfft bins 0..nfft/2 as (hRe, hIm). Returns a Float64Array(nfft).
 * Like pocketfft's c2r, the imaginary parts of the DC and Nyquist bins are dropped.
 */
export function filterReal(x, nfft, hRe, hIm) {
  const plan = fftPlan(nfft);
  const re = new Float64Array(nfft);
  const im = new Float64Array(nfft);
  re.set(x.length > nfft ? x.subarray(0, nfft) : x);
  plan.forward(re, im);
  const half = nfft >> 1;
  const even = (nfft & 1) === 0;
  // DC: rfft gives a real bin
  re[0] = re[0] * hRe[0];
  im[0] = 0;
  const top = even ? half - 1 : half;
  for (let k = 1; k <= top; k++) {
    const r = re[k] * hRe[k] - im[k] * hIm[k];
    const i = re[k] * hIm[k] + im[k] * hRe[k];
    re[k] = r;
    im[k] = i;
    re[nfft - k] = r;
    im[nfft - k] = -i;
  }
  if (even) {
    re[half] = re[half] * hRe[half];
    im[half] = 0;
  }
  plan.inverse(re, im);
  const out = new Float64Array(nfft);
  for (let k = 0; k < nfft; k++) out[k] = re[k] / nfft;
  return out;
}
