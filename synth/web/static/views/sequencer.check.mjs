// node synth/web/static/views/sequencer.check.mjs — exit 0 = the Sequencer view's pure parts hold.
// Covers the grids and beat lines, roll geometry and hit-testing, the four-notes-per-step warning's
// words, the keyboard cursor, bank names, the position words, the view's KeyHint bar, and the one-screen
// layout's contract (round 3; the headless run measures the page itself). The step rules (timing, what a
// step fires, swing, the payload) moved to core/transport.js with the engine: core/transport.check.mjs.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import * as S from "./sequencer.js";
import * as T from "../core/transport.js";

let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };

ok(S.id === "sequencer" && S.title === "Sequencer" && typeof S.mount === "function" && typeof S.unmount === "function", "view contract");
ok(S.default.id === "sequencer" && S.default.unmount === S.unmount, "default export mirrors the named exports");

// ── the grids, and the beat lines (steps per beat from the transport's step timing) ──
eq(S.GRIDS.map((g) => g.value), ["1/8", "1/16", "1/32", "8t", "16t", "32t"], "the grids the cockpit offered");
eq(["1/8", "1/16", "1/32", "8t", "16t", "32t"].map(S.stepsPerBeat), [2, 4, 8, 3, 6, 12], "steps per beat, for the beat lines");
ok(S.GRIDS.every((g) => T.stepSeconds(120, g.value) > 0 && T.stepSeconds(120, g.value) !== 0.5), "every grid the view offers is one the step timing knows");
ok(!("stepSeconds" in S) && !("stepPlan" in S) && !("MAX_STEPS" in S), "the step rules live in one place (core/transport.js), not here too");

// ── the four-notes-per-step warning's words ─────────────────────────────────────
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
ok(S.rollGeometry({ steps: 999, width: 800 }).steps === T.MAX_STEPS, "the roll draws at most the S-1's 64 steps");

// ── keyboard cursor, bank names, position words ─────────────────────────────────
const lim = { steps: 16, lo: 24, hi: 96 };
eq(S.moveCursor({ step: 0, pitch: 60 }, "ArrowLeft", lim), { step: 0, pitch: 60 }, "clamped at the first step");
eq(S.moveCursor({ step: 15, pitch: 60 }, "ArrowRight", lim), { step: 15, pitch: 60 }, "clamped at the last step");
eq(S.moveCursor({ step: 3, pitch: 60 }, "ArrowUp", lim, true), { step: 3, pitch: 72 }, "shift moves an octave");
eq(S.moveCursor({ step: 3, pitch: 30 }, "ArrowDown", lim, true), { step: 3, pitch: 24 }, "clamped at the lowest row");
eq(S.moveCursor({ step: 3, pitch: 60 }, "Enter", lim), { step: 3, pitch: 60 }, "other keys do not move it");
ok(S.validName("Evening loop") && S.validName("  padded  "), "plain names are fine");
ok(![".", "..", ".hidden", "a/b", "a\\b", "", "   "].some(S.validName), "names the app would refuse are caught before the request");
ok(S.positionText({ playing: true, paused: false, position: 4, steps: 16 }) === "Step 5 of 16", "playing");
ok(S.positionText({ playing: true, paused: true, position: 4, steps: 16 }) === "Paused at step 5", "paused");
ok(S.positionText({ playing: false, paused: false, position: -1, steps: 16 }) === "Stopped", "stopped");
ok(S.positionText({ playing: false, paused: true, position: 4, steps: 16 }) === "Paused at step 5", "paused, as the transport says it (playing false)");
ok(S.positionText({ playing: true, paused: false, position: -1, steps: 16 }) === "Starting", "asked to play, no step yet");

// ── the KeyHint bar on this view (app.js shows a view's `hints`) ─────────────────
ok(Array.isArray(S.hints) && S.hints.every((x) => typeof x.key === "string" && x.key && typeof x.label === "string" && x.label), "hints: [{key, label}]");
ok(S.default.hints === S.hints, "the default export carries them too");
const hint = Object.fromEntries(S.hints.map((x) => [x.key, x.label]));
ok(hint.Space === "Play/pause" && hint["⇧ Space"] === "Stop" && hint["?"] === "Keys", "Space Play/pause, ⇧ Space Stop, ? Keys");
ok(hint["− ="] === "Tempo" && S.hints.length === 4, "plus the tempo keys, and no more: one row (the roll's keys are in its caption and the ? list)");
ok(S.hints.every((x) => /^[A-Z]/.test(x.label) && !/→/.test(x.label)), "labels in sentence case, no arrows");

// ── one screen (round 3): what the layout promises, read from the source ─────────
const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "sequencer.js"), "utf8");
for (const a of ["play", "pause", "stop", "delete-note", "save", "switch-pattern"]) ok(src.includes(`"data-action": "${a}"`), `data-action ${a} is kept`);
for (const r of ["poly-warning", "inspector"]) ok(src.includes(`"data-role": "${r}"`), `data-role ${r} is kept`);
ok(src.includes("fitView(view)") && src.includes("fit.destroy()"), "the view fits one screen through core/fit.js and takes its box out on unmount");
ok(src.includes("rect.width / canvas.offsetWidth"), "a click on a scaled-down roll maps back to the unscaled grid");
// At 1470 × 760 the view has 664 px under the header (640 under the static page's intro line). The bar,
// the gap and the paddings take about 126 px, so the roll and its caption line get about 514 px; the
// check keeps 10 px of slack. The right column is exactly that tall; its saved list scrolls inside it.
const px = (name) => Number(new RegExp(`--${name}: (\\d+)px`).exec(src)[1]);
ok(px("roll-h") >= 380 && px("roll-h") + px("under-h") <= 504, `the roll (${px("roll-h")} px) and its caption line (${px("under-h")} px) fit the one-screen budget`);
ok(/\.sq-side \{ height: calc\(var\(--roll-h\) \+ var\(--under-h\)\); overflow: hidden; \}/.test(src), "the right column is as tall as the roll and its caption line, no taller");

console.log(`sequencer view: ${checks} checks passed`);
