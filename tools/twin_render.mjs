#!/usr/bin/env node
// twin_render.mjs — render one note of the browser twin to raw float32, for the parity
// gate (tests/test_twin_parity.py) that holds it to synth/match/twin.py.
//
//   node tools/twin_render.mjs --cc '{"74":60,"20":127}' --note 48 --out note.f32
//        [--path offline|realtime] [--sr 22050] [--seconds 2] [--gate 0.6]
//        [--seed 0] [--stage out|osc|filter|amp] [--curves PATH]
//   node tools/twin_render.mjs --jobs jobs.json      (a JSON list of the same options)
//
// Output: little-endian float32 samples at --sr (default: the twin's own rate).
//   offline   dsp.renderNote — the exact port of Twin.render
//   realtime  dsp.Engine in 128-sample blocks — the code the AudioWorklet runs, with
//             the note-off scheduled at the twin's own gate time (seconds * gate)

import { readFileSync, writeFileSync } from 'node:fs';
import { endianness } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Engine, pyRound, renderNote } from '../synth/web/static/twin/dsp.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_CURVES = resolve(HERE, '../synth/web/static/twin/curves.json');
const BLOCK = 128; // one AudioWorklet render quantum

function usage(msg) {
  if (msg) process.stderr.write(`twin_render: ${msg}\n`);
  process.stderr.write(
    "usage: node tools/twin_render.mjs --cc '{\"74\":60}' --note 48 --out file.f32 " +
      '[--path offline|realtime] [--sr N] [--seconds S] [--gate F] [--seed N] ' +
      '[--stage out|osc|filter|amp] [--curves PATH] | --jobs jobs.json\n',
  );
  process.exit(2);
}

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) usage(`unexpected argument ${a}`);
    const key = a.slice(2);
    const val = argv[i + 1];
    if (val === undefined) usage(`${a} needs a value`);
    i++;
    opts[key] = val;
  }
  return opts;
}

/** Render one job; returns a Float64Array. */
export function renderJob(job, curves) {
  const sr = Number(job.sr ?? curves.sr);
  const seconds = Number(job.seconds ?? curves.seconds);
  const gate = Number(job.gate ?? curves.gate_fraction);
  const note = Number(job.note ?? 60);
  const seed = Number(job.seed ?? 0);
  const stage = job.stage ?? 'out';
  const cc = typeof job.cc === 'string' ? JSON.parse(job.cc) : job.cc || {};
  const path = job.path ?? 'offline';
  if (path === 'offline') {
    const r = renderNote({ cc, curves, note, sr, seconds, gateFraction: gate, seed, stages: true });
    return stage === 'out' ? r.out : r[stage];
  }
  if (path !== 'realtime') throw new Error(`unknown --path ${path}`);
  const eng = new Engine({ sr, curves, modelSr: sr, noiseSeed: seed });
  eng.setAll(cc);
  eng.noteOn(note, 100);
  eng.noteOff(note, seconds * gate); // exact, like the twin's fixed gate
  const n = Math.max(1, pyRound(seconds * sr));
  const out = new Float64Array(n);
  const osc = new Float64Array(BLOCK), filt = new Float64Array(BLOCK), amp = new Float64Array(BLOCK);
  const pick = { osc, filter: filt, amp, out: amp }[stage];
  if (!pick) throw new Error(`unknown --stage ${stage}`);
  for (let pos = 0; pos < n; pos += BLOCK) {
    const m = Math.min(BLOCK, n - pos);
    eng.process(osc, filt, amp, m);
    out.set(pick.subarray(0, m), pos);
  }
  return out;
}

function writeF32(path, data) {
  const f32 = Float32Array.from(data);
  if (endianness() === 'LE') {
    writeFileSync(path, Buffer.from(f32.buffer, f32.byteOffset, f32.byteLength));
    return;
  }
  const buf = Buffer.alloc(f32.length * 4);
  for (let i = 0; i < f32.length; i++) buf.writeFloatLE(f32[i], i * 4);
  writeFileSync(path, buf);
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const curves = JSON.parse(readFileSync(opts.curves || DEFAULT_CURVES, 'utf8'));
  const jobs = opts.jobs ? JSON.parse(readFileSync(opts.jobs, 'utf8')) : [opts];
  for (const job of jobs) {
    if (!job.out) usage('--out is required');
    if (job.cc === undefined) usage('--cc is required');
    const t0 = performance.now();
    const audio = renderJob(job, curves);
    writeF32(job.out, audio);
    if (opts.verbose) {
      process.stderr.write(`${job.out}: ${audio.length} samples in ${(performance.now() - t0).toFixed(1)} ms\n`);
    }
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
