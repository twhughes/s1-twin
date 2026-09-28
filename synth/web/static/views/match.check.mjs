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

// ── round 2: a test of the synth's current sound (ROUND2.md §3) ──────────────────
const py = (rel) => readFileSync(join(here, rel), "utf8");
const matchInit = py("../../../match/__init__.py"), driverPy = py("../../../match/driver.py");
ok(+/ANALYSIS_SECONDS = ([\d.]+)/.exec(matchInit)[1] * +/gate_fraction: float = ([\d.]+)/.exec(twinPy)[1] === M.TEST_GATE,
  "the test note's key goes up when twin.py's render lets go (2.0 s × 0.6)");
ok(+/PROBE_NOTE = (\d+)/.exec(driverPy)[1] === M.TEST_NOTE && M.TEST_SECONDS > M.TEST_GATE, "C3 by default, as the S-1 probe");
eq(/SParam\("lfo_shape", 12, \(([\d, ]+)\)\)/.exec(twinPy)[1].split(",").map(Number).map((v) => M.valueText(12, v)).sort(),
  ["Saw", "Square", "Triangle"], "the LFO waves the matcher tries are the ones the report names");
const curvesJson = JSON.parse(py("../twin/curves.json"));
for (const [name, c] of Object.entries(M.CURVE_DEFAULTS)) {
  const j = curvesJson.curves[name];
  ok(j && j.lo === c.lo && j.hi === c.hi && j.kind === c.kind, `curve ${name} matches curves.json`);
}
ok(M.testNote(new Set()) === 48 && M.testNote(new Set([64, 60])) === 60, "the test plays the lowest marked note, else C3");
ok(M.listText([]) === "" && M.listText(["a"]) === "a" && M.listText(["a", "b", "c"]) === "a, b and c", "lists in words");
ok(M.valueText(28, 0) === "Gate" && M.valueText(12, 2) === "Triangle" && M.valueText(22, 2) === "−1" && M.valueText(74, 90) === "90", "values in words");
ok(M.valueText(76, 64) === "0" && M.valueText(76, 30) === "−34" && M.valueText(76, 70) === "+6", "Fine tune reads as its knob does (from the middle)");
const D = M.truthOf(new Map());
ok(M.TWIN_CCS.every((c) => Number.isFinite(D[c])) && Object.keys(D).length === 21, "the truth holds all 21 settings");
eq(M.truthOf(new Map([[74, 90], [1, 3]]))[74], 90, "…from the synth's params");

// which settings count (relevantCCs): the model's own equations, not a guess
const rel = (p, o) => M.relevantCCs({ ...D, ...p }, o);
const has = (p, cc, o) => rel(p, o).includes(cc);
eq(rel({}, { notes: [48] }), [20, 19, 21, 23, 15, 76, 74, 71, 24, 73, 75, 30, 72, 28], "the S-1 defaults on C3: 14 settings count");
ok(!has({ 19: 0 }, 15) && has({ 19: 1 }, 15), "no pulse width when Square is at 0");
ok(!has({ 21: 0 }, 22) && has({ 21: 40 }, 22), "no sub octave when Sub is at 0");
ok([3, 12, 13, 25, 17].every((cc) => !has({}, cc)), "no LFO settings when every LFO amount is at 0");
ok([3, 12, 13, 25, 17].every((cc) => !has({ 13: 50, 17: 0 }, cc)), "…or when Mod wheel to LFO is at 0");
ok([3, 12, 13, 25].every((cc) => has({ 13: 50 }, cc)) && !has({ 13: 50 }, 17), "vibrato on: the LFO counts, Mod wheel to LFO trades with its amounts");
ok(!has({ 28: 0 }, 75) && !has({ 28: 0 }, 30) && has({ 28: 0 }, 73) && has({ 28: 0 }, 72),
  "Gate and no Env amount: no Decay or Sustain; Attack and Release still shape the gate (twin.py)");
ok(has({ 28: 0, 24: 30 }, 75) && has({ 28: 0, 24: 30 }, 30), "Gate with Env amount: the envelope moves the filter");
ok(!has({ 30: 127 }, 75) && has({ 30: 126 }, 75), "no Decay when Sustain is full");
ok(!has({ 30: 0, 75: 0 }, 72) && has({ 30: 0, 75: 127 }, 72), "no Release when the sound dies away before the key goes up");
ok(has({ 28: 0, 30: 0, 75: 0 }, 72), "…but in Gate mode the release always ends the note");
ok(!has({}, 26, { notes: [48] }) && !has({}, 26, { notes: [60] }) && has({}, 26, { notes: [48, 55] }), "Key follow counts only across notes");
const why = (p, o) => M.relevance({ ...D, ...p }, o).left;
ok(why({}, { notes: [60] }).some((l) => l.ccs.includes(26) && l.kind === "silent" && /C4/.test(l.why)), "at C4 Key follow does nothing");
ok(why({}, { notes: [48] }).some((l) => l.ccs.includes(26) && l.kind === "trade"), "on another note it trades with Cutoff");
eq(M.relevantCCs(new Map(Object.entries({ ...D, 19: 0 }).map(([k, v]) => [+k, v]))), rel({ 19: 0 }), "a Map works like an object");

// the recovery report: true vs found after the model's exact trades
const rep = (t, f, o = {}) => M.recoveryReport({ ...D, ...t }, { ...D, ...f }, { notes: [48], ...o });
const row = (r, cc) => r.rows.find((x) => x.cc === cc);
const same = rep({}, {});
ok(same.good === 14 && same.total === 14 && same.summary === "All 14 settings came back within 10.", "a perfect match: every setting came back");
ok(same.rows.every((x) => x.offText === "same" && !x.traded), "…each the same, nothing traded");
ok(same.notes[0].startsWith("Left out, as they do not change this sound and so cannot come back: Sub octave (Sub is at 0)"),
  "the report says plainly what is left out and why");
const mix = rep({}, { 19: 64 });
ok(row(mix, 19).shown === 127 && row(mix, 19).foundText === "127*" && row(mix, 19).ok, "a quieter mix of the same balance came back (levels as a mix)");
ok(mix.notes.some((n) => n.startsWith("* Compared as the matcher hears them: the levels as a mix")), "…and the trade is named");
const stray = rep({}, { 19: 64, 20: 10 });
ok(!row(stray, 20).ok && row(stray, 20).offText === "off by 20", "a stray level is still a miss after the mix is scaled");
const ortho = rep({ 20: 127, 19: 0 }, { 20: 1, 19: 54, 21: 106, 23: 67, 13: 126, 17: 126, 12: 3 });
ok([20, 19, 21, 23].every((cc) => !row(ortho, cc).ok) && row(ortho, 19).offText === "off by 55" && row(ortho, 21).offText === "off by 109",
  "a mix of the wrong oscillators stays a miss on every level (it is not scaled away)");
ok(row(ortho, 13).offText === "off by 126" && row(ortho, 17).offText === "off by 111" && !row(ortho, 3) && !row(ortho, 12),
  "a vibrato the true sound lacks is a miss; its rate and wave cannot come back");
ok(ortho.notes[0] === "Left out, as they do not change this sound and so cannot come back: Pulse width (Square is at 0); Sub octave (Sub is at 0); Rate and Wave (the true sound has no LFO).",
  "…and the report says why, item by item");
eq(M.relevantCCs({ ...D }), rel({}), "relevantCCs(params) alone: the true sound's own rules");
const kf = rep({ 74: 80 }, { 74: 95, 26: 127 });
ok(row(kf, 74).shown === 80 && row(kf, 74).traded && !row(kf, 26), "Cutoff compared as if Key follow were right (one octave = 14.7 steps)");
const kfRaw = rep({ 74: 80 }, { 74: 95, 26: 127 }, { notes: [48, 55] });
ok(row(kfRaw, 74).off === 15 && row(kfRaw, 26).offText === "off by 127", "across notes both count, untraded");
const lfo = rep({ 13: 40 }, { 13: 20, 17: 30 });
ok(row(lfo, 13).shown === 40 && row(lfo, 13).ok && !row(lfo, 17), "Vibrato compared as if Mod wheel to LFO were right");
ok(row(lfo, 25).shown === 0 && row(lfo, 12).offText === "same", "an LFO amount at 0 still counts once the LFO acts");
const miss = rep({ 13: 40 }, { 13: 40, 12: 3, 71: 30 });
ok(row(miss, 12).offText === "different" && !row(miss, 12).ok && row(miss, 12).trueText === "Triangle" && row(miss, 12).foundText === "Square",
  "switches count only when they match");
ok(row(miss, 71).offText === "off by 30" && miss.summary === "16 of 18 settings came back within 10.", "misses and the one-line summary");
ok(rep({ 71: 20 }, { 71: 30 }, { within: 9 }).summary === "13 of 14 settings came back within 9.", "the bar is a parameter");
ok(rep({ 13: 40, 12: 1 }, { 13: 40 }).notes.some((n) => n.includes("Inverse saw cannot come back")), "a wave the matcher never tries is named");
ok(rep({}, {}, { unison: true }).notes.some((n) => n.startsWith("Unison was on")), "unison is named");
const s1Notes = rep({}, {}, { source: "s1", synced: false }).notes;
ok(s1Notes.some((n) => n.includes("not yet calibrated")) && s1Notes.some((n) => n.includes("No patch was sent")), "an S-1 test says what its numbers can mean");
ok(!rep({}, {}, { source: "s1", synced: true }).notes.some((n) => n.includes("No patch was sent")), "…and drops the patch caveat once synced");
ok(M.recoveryReport(D, { 74: "127", 19: "127" }, { notes: [48] }).good === 14, "frame CC maps with string values work");

// the report shares one fixed-height panel with the knobs: at most 7 rows a table, and short notes
eq([M.tableSplit(0), M.tableSplit(5), M.tableSplit(7), M.tableSplit(8), M.tableSplit(14), M.tableSplit(19), M.tableSplit(21)],
  [[], [5], [7], [4, 4], [7, 7], [7, 6, 6], [7, 7, 7]], "tables of at most 7 rows, as even as they go");
ok(Array.from({ length: 21 }, (_, n) => M.tableSplit(n + 1)).every((s, n) => s.reduce((a, b) => a + b, 0) === n + 1 && Math.max(...s) <= 7),
  "every row lands in exactly one table");
const noteLen = (r) => r.notes.join(" ").length;
ok(noteLen(rep({}, { 19: 64 })) <= 330, `a twin test's notes fit two lines of the panel (${noteLen(rep({}, { 19: 64 }))} characters)`);
const worst = rep({ 21: 40, 13: 40, 12: 1 }, { 19: 64, 26: 90, 13: 20, 17: 30 }, { source: "s1", synced: false, unison: true });
ok(noteLen(worst) <= 700 && worst.rows.length <= 21, `even the longest notes stay within four lines (checked in the browser) (${noteLen(worst)} characters)`);

// the one-screen copy budget (core/fit.js): each line holds its words, so the view keeps to 1470 × 760
for (const k of ["test", "notes", "search"]) ok(M.COPY[k].length <= 52, `COPY.${k} fits one line of the 330 px column (${M.COPY[k].length})`);
ok(M.COPY.intro.length <= 112, `the intro fits two lines (${M.COPY.intro.length})`);
ok(M.COPY.loss.length <= 70 && M.COPY.plume.length <= 40, "each well's caption fits beside its title");
ok(M.COPY.closeness.length <= 150 && M.COPY.honest.length <= 150, "the closeness caveat and the honest line fit one line of the run column");
ok(M.testGoWords([48]) === "C3, from scratch: a minute or two" && M.testGoWords([48]).length <= 36, "a test's words fit beside Match");
ok(Object.values(M.COPY).every((w) => w === w.trim() && /^[A-Z]/.test(w) && !/→/.test(w)), "sentence case, plain, no arrows");

// the pitches a key press sounds: the same as the twin's own voice layer (twin/dsp.js)
const { Engine } = await import("../twin/dsp.js");
for (const p of [{}, { 14: 0 }, { 14: 5 }, { 80: 0 }, { 80: 1 }, { 80: 3 }, { 80: 3, 85: 71, 86: 67, 87: 76 },
  { 80: 3, 81: 0, 85: 71, 86: 67, 87: 76 }, { 80: 3, 14: 3, 85: 52 }]) {
  const eng = new Engine({ sr: curvesJson.sr, curves: curvesJson });
  eng.setAll(p);
  const voices = eng.voicesFor(48);
  const mine = M.soundingNotes(48, new Map(Object.entries(p).map(([k, v]) => [+k, v])));
  eq(mine.notes, [...new Set(voices.map((v) => v.note))].sort((a, b) => a - b), `sounding notes == Engine.voicesFor for ${JSON.stringify(p)}`);
  ok(mine.unison === voices.some((v) => v.detune !== 0), `unison flag for ${JSON.stringify(p)}`);
}

// recording: the input, the words, the level
const devs = [{ deviceId: "default", label: "Default - MacBook Air Microphone" }, { deviceId: "a", label: "MacBook Air Microphone" },
  { deviceId: "s", label: "S-1" }];
ok(M.pickInput(devs) === "s" && M.pickInput(devs, "a") === "a" && M.pickInput(devs, "gone") === "s", "prefer the S-1 unless the user chose another");
ok(M.pickInput(devs.slice(0, 2)) === "default" && M.pickInput([]) === "", "else the default");
ok(M.isS1Label("S-1") && M.isS1Label("Roland S-1 (0582:01b4)") && !M.isS1Label("S-10") && !M.isS1Label("USB-1") && !M.isS1Label(""), "the S-1 by name");
eq(M.inputOptions([{ deviceId: "", label: "" }]), [{ value: "", label: "The default input" }], "before permission the names are hidden");
eq(M.inputOptions(devs.slice(1)).map((o) => o.value), ["a", "s"], "after it, every input");
ok(M.recordError({ name: "NotAllowedError" }) === "The browser blocked the microphone. Allow it in the address bar, then press Record again.",
  "a blocked microphone says what to do");
ok(/Plug one in/.test(M.recordError({ name: "NotFoundError" })) && /another app/i.test(M.recordError({ name: "NotReadableError" })), "no input; a busy input");
ok(/Press Record again\.$/.test(M.recordError(new Error("x"))), "anything else still says what to do");
ok(M.meterLevel(1) === 1 && M.meterLevel(0.001) === 0 && Math.abs(M.meterLevel(10 ** (-30 / 20)) - 0.5) < 1e-9, "the live level: −60..0 dBFS");
ok(M.phaseText(M.initialRun(), { recording: true }).word === "Recording", "recording words");
ok(M.phaseText(M.initialRun(), { recording: "opening" }).word === "Opening the input", "…and while the browser asks");
const mk = { ...M.initialRun(), phase: "making", making: { note: 48, source: "twin" } };
ok(M.phaseText(mk).word === "Making the test note" && M.phaseText(mk).detail === "C3, played by the twin", "making words");
ok(M.phaseText({ ...mk, making: { note: 50, source: "s1" } }).detail === "D3, played by the S-1", "…by the S-1");
ok(M.phaseText({ ...mk, phase: "error", errorWord: "Could not make the test note", error: "x" }).word === "Could not make the test note", "its own error word");

console.log(`match view: ${checks} checks passed`);
