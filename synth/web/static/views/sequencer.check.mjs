// node synth/web/static/views/sequencer.check.mjs — exit 0 = the Sequencer view's pure parts hold.
// Covers step timing (the server's formula), roll geometry and hit-testing, the four-notes-per-step
// warning, what a step fires (gate, probability, swing), and the keyboard cursor.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import * as S from "./sequencer.js";

const here = dirname(fileURLToPath(import.meta.url));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };
const near = (a, b, msg, tol = 1e-9) => ok(Math.abs(a - b) <= tol, `${msg} (${a} vs ${b})`);

ok(S.id === "sequencer" && S.title === "Sequencer" && typeof S.mount === "function" && typeof S.unmount === "function", "view contract");
ok(S.default.id === "sequencer" && S.default.unmount === S.unmount, "default export mirrors the named exports");

// ── device limits agree with the server (synth/sequence.py) ─────────────────────
const seqPy = readFileSync(join(here, "../../../sequence.py"), "utf8");
ok(+/MAX_STEPS = (\d+)/.exec(seqPy)[1] === S.MAX_STEPS, "MAX_STEPS matches sequence.py");
ok(+/MAX_NOTES_PER_STEP = (\d+)/.exec(seqPy)[1] === S.MAX_NOTES_PER_STEP, "MAX_NOTES_PER_STEP matches sequence.py");

// ── step timing: the server's formula for N/D grids; triplets are 2/3 of the straight value ──
const server = (bpm, n, d) => 1 / ((bpm / 60) * (d / (4 * n)));   // sequencer_engine.step_duration_seconds
near(S.stepSeconds(120, "1/16"), 0.125, "1/16 at 120 BPM");
for (const [bpm, n, d] of [[120, 1, 8], [97.5, 1, 16], [60, 1, 32], [200, 1, 4]]) near(S.stepSeconds(bpm, `${n}/${d}`), server(bpm, n, d), `${n}/${d} at ${bpm}`);
near(S.stepSeconds(120, "16t"), 0.125 * 2 / 3, "16t = two thirds of a sixteenth");
near(S.stepSeconds(120, "8t"), 0.25 * 2 / 3, "8t");
near(S.stepSeconds(120, "weird"), 0.5, "an unknown grid is a quarter note, like the server");
near(S.stepSeconds(0, "1/16"), 0.125, "a missing tempo falls back to 120");
eq(S.GRIDS.map((g) => g.value), ["1/8", "1/16", "1/32", "8t", "16t", "32t"], "the grids the cockpit offered");
eq(["1/8", "1/16", "1/32", "8t", "16t", "32t"].map(S.stepsPerBeat), [2, 4, 8, 3, 6, 12], "steps per beat, for the beat lines");

// ── the four-notes-per-step warning ─────────────────────────────────────────────
const five = [60, 62, 64, 65, 67].map((p) => ({ step: 3, pitch: p, velocity: 100, duration: 1 }));
eq(S.polySteps(five), [3], "five notes at step 3");
eq(S.polySteps(five.slice(0, 4)), [], "four is fine");
eq(S.polySteps([...five, ...five.map((n) => ({ ...n, step: 9 })), { step: 0, pitch: 1, velocity: 1, duration: 9 }]), [3, 9], "sorted steps");
ok(S.warningText([8]) === "Step 9 holds more than four notes. The S-1 plays four notes a step at most, so Save to S-1 drops the rest.", "one step, counted from 1");
ok(S.warningText([2, 8, 12]).startsWith("Steps 3, 9 and 13 hold"), "several steps");
ok(S.warningText([]) === "", "no warning");

// ── the roll: geometry and hit-testing ──────────────────────────────────────────
const g = S.rollGeometry({ steps: 16, width: 1360 });
ok(g.cellW === 82 && g.w === 1360 && g.labelW === 48, `wide well: cells grow and the grid meets the edge (${g.cellW}, ${g.w}, ${g.labelW})`);
const phone = S.rollGeometry({ steps: 16, width: 358 });
ok(phone.w === 358 && phone.cellW === 19, `390 px phone: 16 steps fit with no sideways scroll (${phone.w})`);
const long = S.rollGeometry({ steps: 64, width: 358 });
ok(long.cellW === 18 && long.w > 358, "64 steps on a phone keep a usable cell and scroll");
const capped = S.rollGeometry({ steps: 4, width: 1360 });
ok(capped.cellW === 96 && capped.labelW === 44, "few steps: cells stop growing, the key strip stays narrow");
ok(g.rows === 73 && g.h === 73 * g.rowH, "C1..C7 rows");
for (const [step, pitch] of [[0, 96], [15, 24], [7, 60], [3, 61]]) {
  const x = S.stepX(g, step) + g.cellW / 2, y = S.rowY(g, pitch) + g.rowH / 2;
  eq(S.cellAt(g, x, y), { step, pitch }, `cell centre (${step}, ${pitch}) maps back`);
}
ok(S.cellAt(g, g.labelW - 1, 10) === null, "the key strip is not a cell");
ok(S.cellAt(g, g.w + 1, 10) === null && S.cellAt(g, 100, g.h + 1) === null && S.cellAt(g, 100, -1) === null, "off the grid");
const r = S.noteRect(g, { step: 2, pitch: 60, duration: 3 });
ok(r.x === S.stepX(g, 2) + 1 && r.w === 3 * g.cellW - 2 && r.y === S.rowY(g, 60) + 1 && r.h === g.rowH - 2, "note rectangle");
const notes = [{ step: 2, pitch: 60, velocity: 90, duration: 3 }];
ok(S.noteAt(notes, 4, 60) === notes[0] && S.noteAt(notes, 5, 60) === null && S.noteAt(notes, 3, 61) === null, "a held note covers its steps");
ok(S.clampDuration(9, 12, 16) === 4 && S.clampDuration(0, 3, 16) === 1 && S.clampDuration(2.4, 0, 16) === 2, "lengths stay inside the pattern");

// ── what a step fires: gate, probability, swing (the server's rules) ────────────
const plan = S.stepPlan([{ step: 1, pitch: 60, velocity: 80, duration: 2 }, { step: 2, pitch: 64, velocity: 90, duration: 1 }],
  1, { gate: 0.5, probability: 1, stepSec: 0.125 }, () => 0.3);
eq(plan, [{ pitch: 60, velocity: 80, hold: 0.125 }], "hold = duration × gate steps");
eq(S.stepPlan([{ step: 0, pitch: 60, velocity: 80, duration: 1 }], 0, { gate: 0.01, stepSec: 0.1 }, () => 0)[0].hold, 0.05 * 0.1, "never shorter than 0.05 of a step");
eq(S.stepPlan(notes, 2, { probability: 0.25 }, () => 0.5), [], "a failed probability roll fires nothing");
eq(S.stepPlan(notes, 2, { probability: 0 }, () => 0), [], "probability 0 never fires");
ok(S.swingDelay(1, 0.5, 0.2) === 0.1 && S.swingDelay(2, 0.5, 0.2) === 0, "swing delays the off-beat (odd index) steps");

// ── keyboard cursor, payload, position words ────────────────────────────────────
const lim = { steps: 16, lo: 24, hi: 96 };
eq(S.moveCursor({ step: 0, pitch: 60 }, "ArrowLeft", lim), { step: 0, pitch: 60 }, "clamped at the first step");
eq(S.moveCursor({ step: 15, pitch: 60 }, "ArrowRight", lim), { step: 15, pitch: 60 }, "clamped at the last step");
eq(S.moveCursor({ step: 3, pitch: 60 }, "ArrowUp", lim, true), { step: 3, pitch: 72 }, "shift moves an octave");
eq(S.moveCursor({ step: 3, pitch: 30 }, "ArrowDown", lim, true), { step: 3, pitch: 24 }, "clamped at the lowest row");
eq(S.moveCursor({ step: 3, pitch: 60 }, "Enter", lim), { step: 3, pitch: 60 }, "other keys do not move it");
eq(S.sequencePayload({ steps: 8, bpm: 99, step_resolution: "1/8", notes: [{ step: 1, pitch: 60, velocity: 70, duration: 2, extra: 1 }], poly_warnings: [] }),
  { steps: 8, bpm: 99, step_resolution: "1/8", notes: [{ step: 1, pitch: 60, velocity: 70, duration: 2 }] }, "only the fields the server accepts");
ok(S.validName("Evening loop") && S.validName("  padded  "), "plain names are fine");
ok(![".", "..", ".hidden", "a/b", "a\\b", "", "   "].some(S.validName), "names the app would refuse are caught before the request");
ok(S.positionText({ playing: true, paused: false, position: 4, steps: 16 }) === "Step 5 of 16", "playing");
ok(S.positionText({ playing: true, paused: true, position: 4, steps: 16 }) === "Paused at step 5", "paused");
ok(S.positionText({ playing: false, paused: false, position: -1, steps: 16 }) === "Stopped", "stopped");

console.log(`sequencer view: ${checks} checks passed`);
