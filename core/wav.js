// core/wav.js — targets made in the browser (a recording, or a test note of the synth's current
// sound): join the recorded chunks, trim the silence before the first onset, and write a 16-bit
// mono PCM WAV that the server reads like any dropped file (synth/match/twin_session.decode_upload).
// Pure: no DOM, no Web Audio. `node core/wav.check.mjs` checks it (W-rec, docs/design/ROUND2.md §3).

/** 16-bit PCM mono WAV bytes (RIFF) of `samples` (floats; clamped to −1..1, NaN → 0) at `sampleRate`. */
export function encodeWav(samples, sampleRate) {
  const n = samples.length, sr = Math.round(sampleRate);
  const buf = new ArrayBuffer(44 + 2 * n), v = new DataView(buf);
  const text = (at, s) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); v.setUint32(4, 36 + 2 * n, true); text(8, "WAVE");
  text(12, "fmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);          // PCM
  v.setUint16(22, 1, true);          // mono
  v.setUint32(24, sr, true);
  v.setUint32(28, sr * 2, true);     // bytes a second
  v.setUint16(32, 2, true);          // bytes a frame
  v.setUint16(34, 16, true);         // bits a sample
  text(36, "data"); v.setUint32(40, 2 * n, true);
  for (let i = 0; i < n; i++) {
    const x = Math.max(-1, Math.min(1, Number(samples[i]) || 0));
    v.setInt16(44 + 2 * i, Math.round(x < 0 ? x * 32768 : x * 32767), true);
  }
  return buf;
}

/** One Float32Array from the recorder's chunks, at most `max` samples long. */
export function joinChunks(chunks, max = Infinity) {
  const total = Math.min(max, chunks.reduce((a, c) => a + c.length, 0));
  const out = new Float32Array(total);
  let at = 0;
  for (const c of chunks) {
    if (at >= total) break;
    const take = Math.min(c.length, total - at);
    out.set(take === c.length ? c : c.subarray(0, take), at);
    at += take;
  }
  return out;
}

export function peakOf(samples) {
  let m = 0;
  for (let i = 0; i < samples.length; i++) { const a = Math.abs(samples[i]); if (a > m) m = a; }
  return m;
}

/** A copy scaled so its peak is `peak` (silence stays silence). */
export function normalize(samples, peak = 0.9) {
  const p = peakOf(samples), g = p > 0 ? peak / p : 0;
  const out = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) out[i] = samples[i] * g;
  return out;
}

/**
 * Where the sound starts, as a sample index: the first ~1 ms window that rises above both
 * `thresholdDb` under the loudest window (the server's rule, capture.find_onset) and `overFloorDb`
 * over the quiet floor (the 10th percentile window, so a noisy room does not count as the note),
 * and stays up: the median of the next `holdMs` windows is above too, so a click is not a note.
 * 0 when nothing qualifies (the caller then keeps everything).
 */
export function onsetIndex(samples, sampleRate, { thresholdDb = -45, overFloorDb = 10, holdMs = 20 } = {}) {
  const win = Math.max(1, Math.round(sampleRate / 1000));
  const nWin = Math.floor(samples.length / win);
  if (nWin < 1) return 0;
  const rms = new Float64Array(nWin);
  let peak = 0;
  for (let w = 0; w < nWin; w++) {
    let s = 0;
    for (let i = w * win, e = i + win; i < e; i++) s += samples[i] * samples[i];
    rms[w] = Math.sqrt(s / win);
    if (rms[w] > peak) peak = rms[w];
  }
  if (!(peak > 0)) return 0;
  const floor = Float64Array.from(rms).sort()[Math.floor(0.1 * (nWin - 1))];
  const thr = Math.max(peak * 10 ** (thresholdDb / 20), floor * 10 ** (overFloorDb / 20));
  const hold = Math.max(1, Math.round((holdMs / 1000) * sampleRate / win));
  for (let w = 0; w < nWin; w++) {
    if (rms[w] < thr) continue;
    const next = Array.from(rms.subarray(w, Math.min(nWin, w + hold))).sort((a, b) => a - b);
    if (next[Math.floor(next.length / 2)] >= thr) return w * win;
  }
  return 0;
}

/** `samples` from `keepMs` before the onset (a new array, so the recorder's buffers can go). */
export function trimToOnset(samples, sampleRate, { keepMs = 5, ...opts } = {}) {
  const at = onsetIndex(samples, sampleRate, opts);
  const start = Math.max(0, at - Math.round((keepMs / 1000) * sampleRate));
  return Float32Array.from(samples.subarray ? samples.subarray(start) : samples.slice(start));
}
