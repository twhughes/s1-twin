// node synth/web/static/views/match.check.mjs — exit 0 = the Match view's pure parts hold.
// Covers the frame reducer, the phase words, the replay schedule, the loss scale, and the
// drift between the view's controls and the twin's parameters (synth/match/twin.py).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import * as M from "./match.js";

const here = dirname(fileURLToPath(import.meta.url));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };

// ── the view's controls are exactly the twin's 18 knobs + 3 switches (drift check) ──
const twinPy = readFileSync(join(here, "../../../match/twin.py"), "utf8");
const kccs = [...twinPy.matchAll(/KParam\("\w+", (\d+)\)/g)].map((m) => +m[1]);
const sccs = [...twinPy.matchAll(/SParam\("\w+", (\d+),/g)].map((m) => +m[1]);
ok(kccs.length === 18 && sccs.length === 3, `twin.py declares 18 k + 3 s params (${kccs.length} + ${sccs.length})`);
eq([...M.TWIN_CCS].sort((a, b) => a - b), [...kccs, ...sccs].sort((a, b) => a - b), "view controls == twin params");
ok(new Set(M.TWIN_CCS).size === M.TWIN_CCS.length, "no control twice");
const switches = M.STAGES.flatMap((s) => s.controls).filter((c) => c.options || c.glyphs).map((c) => c.cc).sort((a, b) => a - b);
eq(switches, [...sccs].sort((a, b) => a - b), "the switches are the twin's discrete params");
eq(M.STAGES.map((s) => s.name), ["Oscillator", "Filter", "Envelope", "LFO"], "signal order");
ok(M.id === "match" && M.title === "Match" && typeof M.mount === "function" && typeof M.unmount === "function", "view contract");
ok(M.default.id === "match" && M.default.mount === M.mount, "default export mirrors the named exports");

// ── a synthetic stream: pitch → gd (2 starts) → note-search → done ───────────────
const cc = (v) => Object.fromEntries(M.TWIN_CCS.map((c) => [String(c), v]));
const wave = { spc: 64, y: new Array(132).fill(0), level: 1 };
const base = { notes: [60], chord_name: "C4", note: 60, note_name: "C4" };
const frames = [
  { phase: "pitch", ...base, seeded: false, iter: 0, total: 4, restart: 0, loss: 8, best_loss: 8, cc: cc(1), wave, target_wave: wave },
  { phase: "gd", ...base, iter: 1, total: 4, restart: 0, loss: 6, best_loss: 6, cc: cc(2), wave },
  { phase: "gd", ...base, iter: 2, total: 4, restart: 0, loss: 7, best_loss: 6, cc: cc(3), wave },
  { phase: "gd", ...base, iter: 3, total: 4, restart: 1, loss: 9, best_loss: 6, cc: cc(4), wave },
  { phase: "gd", ...base, iter: 4, total: 4, restart: 1, loss: 5, best_loss: 5, cc: cc(5), wave },
  { phase: "note-search", notes: [48], chord_name: "C3", note: 48, note_name: "C3", iter: 1, total: 2, loss: 5.5, best_loss: 5, improved: false, cc: cc(6), wave },
  { phase: "note-search", notes: [60, 67], chord_name: "C4+G4", note: 60, note_name: "C4", iter: 2, total: 2, loss: 4, best_loss: 4, improved: true, cc: cc(7), wave },
  { phase: "done", notes: [60, 67], chord_name: "C4+G4", note: 60, note_name: "C4", seeded: false, iter: 4, total: 4, restart: 2,
    loss: 3.9, best_loss: 3.9, cc: cc(8), wave, target_wave: wave, closeness: 71.4, seconds: 12.34, steps: 25,
    match_wav_b64: "AAAA", target_wav_b64: "BBBB" },
];
const start = M.initialRun();
const frozen = JSON.stringify(start);
let s = start;
const states = frames.map((f) => (s = M.reduceFrame(s, f)));
ok(JSON.stringify(start) === frozen, "reduceFrame never mutates its input");
ok(s.phase === "done" && s.count === frames.length, "every frame counted");
ok(s.points.length === frames.length - 1, "one curve point per step (done is a summary, not a step)");
eq(s.marks, [{ i: 3, label: "Start 2" }, { i: 5, label: "Nearby notes" }], "leaders at the second start and the note search");
eq(states[2].bestCC, cc(2), "a worse step keeps the best candidate");
eq(states[4].bestCC, cc(5), "a new best replaces it");
eq(states[6].bestCC, cc(7), "the note search can set the best too");
const wv = (v) => ({ spc: 64, y: [v], level: 1 });
const wstates = [frames[0], { ...frames[1], wave: wv(1) }, { ...frames[2], wave: wv(2) }].reduce((acc, f) => [...acc, M.reduceFrame(acc.at(-1) || start, f)], []);
eq(wstates[2].bestWave, wv(1), "the best candidate's cycles are kept for a stopped run's plume");
eq(s.done.cc, cc(8), "done carries the final patch (after the switches)");
ok(s.done.closeness === 71.4 && s.done.seconds === 12.34 && s.done.steps === 25, "done numbers");
ok(s.done.matchWav === "AAAA" && s.done.targetWav === "BBBB", "done WAVs");
eq(s.notes, [60, 67], "the final note set");
ok(states[0].seeded === false && states[0].targetWave === wave, "pitch frame: seeded flag and the target's cycles");
ok(M.expectedSteps(states[1]) === 5, "the x axis holds the announced steps (pitch + 4)");
ok(M.expectedSteps(states[6]) === 7, "…and grows when the note search announces its steps");
ok(M.expectedSteps({ points: new Array(130), total: 640, frameCount: 130 }) === 130, "a thinned recording fills the axis");
{ // the plume's loop: averaged, 4x finer, closed, and still the same wave
  const spc = 64, y = Array.from({ length: 132 }, (_, i) => Math.sin(2 * Math.PI * i / spc));
  const L = M.smoothLoop({ spc, y });
  ok(L && L.spc === 256 && L.y.length === 768 && L.from === 256, "smoothLoop tiles one 4x cycle three times");
  const err = Math.max(...Array.from({ length: 256 }, (_, i) => Math.abs(L.y[256 + i] - Math.sin(2 * Math.PI * i / 256))));
  ok(err < 0.05, `smoothLoop keeps a clean sine (max error ${err.toFixed(3)})`);
  ok(M.smoothLoop({ spc: 64, y: [0, 1] }) === null, "no full cycle, no loop");
}
const err = M.reduceFrame(states[2], { phase: "error", detail: "Could not read that file as audio." });
ok(err.phase === "error" && err.error.includes("audio") && err.points.length === states[2].points.length, "an error frame keeps the curve");
ok(M.reduceFrame(start, null) === start && M.reduceFrame(start, "x") === start, "junk frames are ignored");

// ── the phase in plain words ─────────────────────────────────────────────────────
const word = (st, o) => M.phaseText(st, o).word;
ok(word(start) === "Waiting for a sound" && word(start, { loaded: true }) === "Ready to match", "idle words");
ok(word(start, { staticMode: true }) === "Choose a recorded run", "idle, static page");
ok(word(states[0]) === "Finding the notes" && M.phaseText(states[0]).detail === "Found: C4", "pitch, cold start");
const seededPitch = M.reduceFrame(start, { ...frames[0], seeded: true, notes: [55, 59, 62], chord_name: "G3+B3+D4" });
ok(M.phaseText(seededPitch).detail === "Marked: G3 + B3 + D4", "pitch, seeded, chord spelled with spaces");
ok(word(states[1]) === "Descending" && M.phaseText(states[1]).detail === "Step 1 of 4", "gd words");
ok(M.phaseText(states[3]).detail === "Step 3 of 4, start 2", "gd names the start after the first");
const lastSeeded = M.reduceFrame(M.reduceFrame(start, { ...frames[0], seeded: true }), frames[4]);
ok(M.phaseText(lastSeeded).detail.endsWith("choosing the switches"), "the silent switch sweep after the last step is named");
ok(word(states[5]) === "Trying nearby notes" && M.phaseText(states[6]).detail.startsWith("C4 + G4, closer"), "note-search words");
ok(word(s) === "Done" && M.phaseText(s).detail === "C4 + G4, 12.3 s, 25 steps", "done words");
ok(word({ ...s, phase: "stopped" }) === "Stopped" && word(err) === "Could not match", "stopped and error words");
ok(M.prettyChord("C#4+F#4") === "C♯4 + F♯4", "sharps print as ♯");
ok(M.fmtSeconds(7.25) === "7.3 s" && M.fmtSeconds(135) === "2 min 15 s" && M.fmtSeconds(NaN) === "", "durations");

// ── replay timing for recorded runs ─────────────────────────────────────────────
const t = M.replaySchedule(frames, { total: 1400, minGap: 10, maxGap: 200, pause: 300 });
ok(t.length === frames.length && t[0] === 0, "one time per frame, from zero");
ok(t.every((v, i) => i === 0 || v > t[i - 1]), "strictly increasing");
const gap = 1400 / (frames.length - 1);
ok(Math.abs(t[1] - t[0] - (gap + 300)) < 1e-9, "a pause where the phase changes (pitch → gd)");
ok(Math.abs(t[2] - t[1] - gap) < 1e-9, "no pause inside a phase");
ok(Math.abs(t[3] - t[2] - (gap + 300)) < 1e-9, "a pause at a new start");
const long = Array.from({ length: 700 }, (_, i) => ({ phase: "gd", restart: 0, loss: 1, best_loss: 1, iter: i }));
const tl = M.replaySchedule(long);
ok(tl[tl.length - 1] <= 12000 + 1e-6 && tl[1] - tl[0] >= 12, "long runs squeeze into ~12 s, never below the minimum gap");
eq(M.replaySchedule([]), [], "an empty run has no schedule");

// ── the loss scale, the plume levels, the recorded index, the warm start ─────────
const d = M.lossDomain(s.points);
ok(d.lo < 4 && d.hi > 9 && d.lo > 0, "log domain covers every loss and best point");
const flat = M.lossDomain([{ loss: 2, best: 2 }]);
ok(flat.lo < 2 && flat.hi > 2 && flat.hi / flat.lo > 1.2, "a flat curve still gets a usable range");
eq(M.lossDomain([]), { lo: 0.1, hi: 1 }, "no points: a default range");
eq(M.plumeLevels({ level: 2 }, { level: 1 }), { target: 1, cand: 0.5 }, "the louder loop fills the well");
eq(M.plumeLevels({ level: 1 }, { level: 0.01 }), { target: 1, cand: 0.12 }, "a near-silent guess stays visible");
eq(M.plumeLevels(null, { level: 1 }), { target: 0, cand: 1 }, "no target yet");
eq(M.indexRows([{ slug: "a3-saw", title: "A3 saw", notes: [57], closeness: 64.2 }, { slug: "../etc", title: "x" }, null]).map((r) => r.slug),
  ["a3-saw"], "index rows: safe slugs only");
ok(M.indexRows({ matches: [{ slug: "b" }] })[0].title === "b" && M.indexRows("nope").length === 0, "index: {matches: […]} or nothing");
eq(M.initMap(new Map([[74, 90], [1, 5], [22, 1]])), { 74: 90, 22: 1 }, "warm start sends only the twin's CCs");

console.log(`match view: ${checks} checks passed`);
