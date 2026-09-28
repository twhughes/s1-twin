// node synth/web/static/core/wav.check.mjs — exit 0 = the browser's WAV writer holds.
// Reads the bytes back by hand (not with wav.js), so a wrong header or sample scale cannot hide.
// tests/test_match_record.py also decodes these bytes with the server's own reader.
import assert from "node:assert/strict";

import * as W from "./wav.js";

const { encodeWav, joinChunks, peakOf, normalize } = W;

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

// ── no trim here: the server finds the sound in the whole take (synth/match/target_prep.py) ─
ok(!("trimToOnset" in W) && !("onsetIndex" in W), "the browser never crops a take: one place decides");

console.log(`wav: ${checks} checks passed`);
