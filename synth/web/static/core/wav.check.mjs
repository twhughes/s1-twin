// node synth/web/static/core/wav.check.mjs — exit 0 = the browser's WAV writer and onset trim hold.
// Reads the bytes back by hand (not with wav.js), so a wrong header or sample scale cannot hide.
// tests/test_match_record.py also decodes these bytes with the server's own reader.
import assert from "node:assert/strict";

import { encodeWav, joinChunks, peakOf, normalize, onsetIndex, trimToOnset } from "./wav.js";

let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };

// ── the header: RIFF / WAVE / fmt (PCM, mono, 16-bit) / data ─────────────────────
const sr = 22050;
const buf = encodeWav(Float32Array.from([0, 0.5, -0.5, 1, -1, 2, -3, NaN]), sr);
const v = new DataView(buf);
const str = (at, n) => String.fromCharCode(...new Uint8Array(buf, at, n));
ok(buf.byteLength === 44 + 2 * 8, "44-byte header + 2 bytes a sample");
ok(str(0, 4) === "RIFF" && v.getUint32(4, true) === buf.byteLength - 8 && str(8, 4) === "WAVE", "RIFF chunk and size");
ok(str(12, 4) === "fmt " && v.getUint32(16, true) === 16, "a 16-byte fmt chunk");
ok(v.getUint16(20, true) === 1 && v.getUint16(22, true) === 1, "PCM, one channel");
ok(v.getUint32(24, true) === sr && v.getUint32(28, true) === sr * 2, "sample rate and byte rate");
ok(v.getUint16(32, true) === 2 && v.getUint16(34, true) === 16, "block align 2, 16 bits");
ok(str(36, 4) === "data" && v.getUint32(40, true) === 16, "data chunk size");
const pcm = Array.from({ length: 8 }, (_, i) => v.getInt16(44 + 2 * i, true));
eq(pcm, [0, 16384, -16384, 32767, -32768, 32767, -32768, 0], "scaled, clamped, NaN is silence");
ok(encodeWav(new Float32Array(0), 48000).byteLength === 44, "an empty take is a valid, empty WAV");
ok(new DataView(encodeWav([0.25], 44100.4)).getUint32(24, true) === 44100, "the rate is a whole number");

// ── chunks, peak, normalize ─────────────────────────────────────────────────────
const joined = joinChunks([Float32Array.from([1, 2]), Float32Array.from([3]), Float32Array.from([4, 5])]);
eq(Array.from(joined), [1, 2, 3, 4, 5], "chunks join in order");
eq(Array.from(joinChunks([Float32Array.from([1, 2, 3]), Float32Array.from([4, 5])], 4)), [1, 2, 3, 4], "…and stop at the cap");
ok(joinChunks([]).length === 0, "no chunks, no samples");
ok(peakOf(Float32Array.from([0.1, -0.7, 0.3])) === Float32Array.from([0.7])[0], "the peak is the largest magnitude");
const nrm = normalize(Float32Array.from([0.1, -0.2]), 0.9);
ok(Math.abs(peakOf(nrm) - 0.9) < 1e-6 && Math.abs(nrm[0] - 0.45) < 1e-6, "normalize scales to the peak asked for");
ok(normalize(new Float32Array(4)).every((x) => x === 0), "silence stays silence");

// ── the onset: silence, a note, and the rules that keep a click or room noise out ─
const rate = 48000;
function take({ lead = 0.5, note = 0.6, amp = 0.5, noise = 0, click = null, seed = 1 } = {}) {
  const n = Math.round((lead + note + 0.2) * rate), x = new Float32Array(n);
  let s = seed;
  const rnd = () => { s = (s * 16807) % 2147483647; return s / 2147483647 - 0.5; };
  for (let i = 0; i < n; i++) {
    const t = i / rate - lead;
    x[i] = noise * rnd();
    if (t >= 0 && t < note) x[i] += amp * Math.sin(2 * Math.PI * 130.8 * t) * Math.min(1, t / 0.002);
  }
  if (click != null) for (let i = 0; i < 0.003 * rate; i++) x[Math.round(click * rate) + i] += (i % 2 ? -0.9 : 0.9);
  return x;
}
const ms = (i) => (i / rate) * 1000;
const at = onsetIndex(take(), rate);
ok(Math.abs(ms(at) - 500) <= 2, `a note after silence starts at 500 ms (${ms(at).toFixed(1)})`);
const trimmed = trimToOnset(take(), rate, { keepMs: 5 });
ok(Math.abs(ms(take().length - trimmed.length) - 495) <= 2, "the trim keeps about 5 ms before the onset");
ok(trimmed instanceof Float32Array && trimmed.length < take().length, "a new, shorter array");
const clicky = onsetIndex(take({ click: 0.2 }), rate);
ok(Math.abs(ms(clicky) - 500) <= 2, `a click before the note is not the onset (${ms(clicky).toFixed(1)} ms)`);
const noisy = onsetIndex(take({ noise: 0.01, amp: 0.3 }), rate);
ok(Math.abs(ms(noisy) - 500) <= 3, `room noise is not the onset (${ms(noisy).toFixed(1)} ms)`);
ok(onsetIndex(take({ lead: 0 }), rate) === 0, "a note from the first sample is kept whole");
ok(onsetIndex(new Float32Array(rate), rate) === 0 && trimToOnset(new Float32Array(100), rate).length === 100, "silence: nothing is cut");
ok(onsetIndex(new Float32Array(10), rate) === 0, "shorter than a window: nothing is cut");
ok(trimToOnset(Array.from(take()), rate).length === trimmed.length, "plain arrays work too");

console.log(`wav: ${checks} checks passed`);
