// node synth/web/static/core/transport.check.mjs — exit 0 = the page's one transport keeps its contract
// (docs/design/ROUND2.md §2): the app's step rules, toggle semantics, the static clock's step order and
// timing on a fake timer (swing, tempo changes, a throttled tab, pause and resume, stop), voicing through
// ctx.note (the keys light; nothing sounds while the S-1 does), and following the app (one request at a
// time, the /ws/state messages, edits pushed after a pause, echoes held off while an edit is live).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import * as T from "./transport.js";
import { createCtx } from "./ctx.js";

const here = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(readFileSync(join(here, "schema.json"), "utf8"));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };
const near = (a, b, msg, tol = 1e-9) => ok(Math.abs(a - b) <= tol, `${msg} (${a} vs ${b})`);
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r)); };
const range = (a, b) => Array.from({ length: b - a }, (_, i) => a + i);

/** setTimeout on a fake clock: advance(ms) runs what falls due, in time order; jump(ms) moves time
 *  without running anything (a throttled background tab). */
function fakeClock() {
  let t = 0, id = 0;
  const q = new Map();
  return {
    now: () => t,
    later: (fn, ms) => { const k = ++id; q.set(k, { at: t + Math.max(0, ms), fn }); return k; },
    cancel: (k) => { q.delete(k); },
    advance(ms) {
      const end = t + ms;
      for (;;) {
        let best = null;
        for (const [k, v] of q) if (v.at <= end && (!best || v.at < best[1].at)) best = [k, v];
        if (!best) break;
        q.delete(best[0]);
        t = Math.max(t, best[1].at);                  // a timer that is overdue runs late, now
        best[1].fn();
      }
      t = end;
    },
    jump(ms) { t += ms; },
    pending: () => q.size,
  };
}
function fakeTwin() {
  const calls = [];
  return {
    calls,
    set: () => {}, setAll: () => {},
    noteOn: (n, v) => calls.push(["on", n, v]), noteOff: (n) => calls.push(["off", n]),
    allOff: () => calls.push(["allOff"]), resume: () => { calls.push(["resume"]); return Promise.resolve(); },
    modeled: () => true,
  };
}

// ── the app's rules (moved here from views/sequencer.js; they agree with the server) ─────
const seqPy = readFileSync(join(here, "../../../sequence.py"), "utf8");
ok(+/MAX_STEPS = (\d+)/.exec(seqPy)[1] === T.MAX_STEPS, "MAX_STEPS matches sequence.py");
ok(+/MAX_NOTES_PER_STEP = (\d+)/.exec(seqPy)[1] === T.MAX_NOTES_PER_STEP, "MAX_NOTES_PER_STEP matches sequence.py");

const server = (bpm, n, d) => 1 / ((bpm / 60) * (d / (4 * n)));   // sequencer_engine.step_duration_seconds
near(T.stepSeconds(120, "1/16"), 0.125, "1/16 at 120 BPM");
for (const [bpm, n, d] of [[120, 1, 8], [97.5, 1, 16], [60, 1, 32], [200, 1, 4]]) near(T.stepSeconds(bpm, `${n}/${d}`), server(bpm, n, d), `${n}/${d} at ${bpm}`);
near(T.stepSeconds(120, "16t"), 0.125 * 2 / 3, "16t = two thirds of a sixteenth");
near(T.stepSeconds(120, "8t"), 0.25 * 2 / 3, "8t");
near(T.stepSeconds(120, "weird"), 0.5, "an unknown grid is a quarter note, like the server");
near(T.stepSeconds(0, "1/16"), 0.125, "a missing tempo falls back to 120");

const five = [60, 62, 64, 65, 67].map((p) => ({ step: 3, pitch: p, velocity: 100, duration: 1 }));
eq(T.polySteps(five), [3], "five notes at step 3");
eq(T.polySteps(five.slice(0, 4)), [], "four is fine");
eq(T.polySteps([...five, ...five.map((n) => ({ ...n, step: 9 })), { step: 0, pitch: 1, velocity: 1, duration: 9 }]), [3, 9], "sorted steps");
ok(T.clampDuration(9, 12, 16) === 4 && T.clampDuration(0, 3, 16) === 1 && T.clampDuration(2.4, 0, 16) === 2, "lengths stay inside the pattern");

const plan = T.stepPlan([{ step: 1, pitch: 60, velocity: 80, duration: 2 }, { step: 2, pitch: 64, velocity: 90, duration: 1 }],
  1, { gate: 0.5, probability: 1, stepSec: 0.125 }, () => 0.3);
eq(plan, [{ pitch: 60, velocity: 80, hold: 0.125 }], "hold = duration × gate steps");
eq(T.stepPlan([{ step: 0, pitch: 60, velocity: 80, duration: 1 }], 0, { gate: 0.01, stepSec: 0.1 }, () => 0)[0].hold, 0.05 * 0.1, "never shorter than 0.05 of a step");
const one = [{ step: 2, pitch: 60, velocity: 90, duration: 3 }];
eq(T.stepPlan(one, 2, { probability: 0.25 }, () => 0.5), [], "a failed probability roll fires nothing");
eq(T.stepPlan(one, 2, { probability: 0 }, () => 0), [], "probability 0 never fires");
ok(T.swingDelay(1, 0.5, 0.2) === 0.1 && T.swingDelay(2, 0.5, 0.2) === 0, "swing delays the off-beat (odd index) steps");
eq(T.sequencePayload({ steps: 8, bpm: 99, step_resolution: "1/8", notes: [{ step: 1, pitch: 60, velocity: 70, duration: 2, extra: 1 }], poly_warnings: [] }),
  { steps: 8, bpm: 99, step_resolution: "1/8", notes: [{ step: 1, pitch: 60, velocity: 70, duration: 2 }] }, "only the fields the server accepts");

// ── toggle semantics: playing and not paused -> pause, else play ─────────────────────
eq(T.toggleAction({ playing: false, paused: false }), "play", "stopped: Space plays");
eq(T.toggleAction({ playing: true, paused: false }), "pause", "running: Space pauses");
eq(T.toggleAction({ playing: false, paused: true }), "play", "paused (the app's word): Space resumes");
eq(T.toggleAction({ playing: true, paused: true }), "play", "playing and paused: Space resumes");
eq(T.toggleAction({ playing: false, paused: false }, "play"), "pause", "a play on its way counts as running (a quick second press pauses)");
eq(T.toggleAction({ playing: true, paused: false }, "pause"), "play", "a pause on its way counts as paused");
eq(T.toggleAction({ playing: true, paused: false }, "stop"), "play", "a stop on its way counts as stopped");

// ── the static clock: step order and timing on a fake timer ──────────────────────────
function clockRig({ bpm = 120, steps = 16, shuffle = 0 } = {}) {
  const c = fakeClock(), fired = [];
  const knobs = { bpm, steps, shuffle };
  const clock = T.createClock({ now: c.now, later: c.later, cancel: c.cancel,
    stepSec: () => T.stepSeconds(knobs.bpm, "1/16"), steps: () => knobs.steps, shuffle: () => knobs.shuffle,
    onStep: (s) => fired.push([c.now(), s]) });
  return { c, clock, fired, knobs };
}
{
  const { c, clock, fired } = clockRig();
  clock.start(true);
  eq(fired, [[0, 0]], "Play fires step 1 at once");
  c.advance(2000);
  eq(fired.map(([, s]) => s), [...range(0, 16), 0], "steps run in order, then round again");
  ok(fired.every(([t], i) => t === i * 125), "each on its grid: 125 ms apart at 120 BPM on 1/16");
  clock.halt(false);
  const at = fired.length;
  c.advance(1000);
  ok(fired.length === at && c.pending() === 0, "halted: nothing fires and no timer is left");
  clock.start(false);
  eq(fired.at(-1), [3000, 1], "on from the pause: the next step (2), not step 1");
  clock.halt(true);
  clock.start(false);
  eq(fired.at(-1)[1], 0, "after a reset it starts at step 1");
}
{
  const { c, clock, fired } = clockRig({ shuffle: 0.5 });
  clock.start(true);
  c.advance(1000);
  eq(fired.slice(0, 6), [[0, 0], [187.5, 1], [250, 2], [437.5, 3], [500, 4], [687.5, 5]], "shuffle 0.5: odd steps land half a step late, even steps stay on the grid");
}
{
  const { c, clock, fired, knobs } = clockRig();
  clock.start(true);
  c.advance(1000);                                   // steps 1 to 9 at 0 … 1000 ms
  knobs.bpm = 60;
  c.advance(1000);
  eq(fired.slice(9), [[1125, 9], [1375, 10], [1625, 11], [1875, 12]], "a tempo change: the step already set keeps its time, then 250 ms apart (no jump)");
  knobs.steps = 4;
  c.advance(250);
  eq(fired.at(-1), [2125, 0], "a shorter pattern wraps at once to step 1");
}
{
  const { c, clock, fired } = clockRig();
  clock.start(true);
  c.advance(250);                                     // steps 1, 2, 3
  c.jump(5000);                                       // the tab slept: 40 steps went by
  c.advance(0);
  eq(fired.length, 4, "a late tick plays one step, not the 40 it missed");
  c.advance(125);
  eq(fired.at(-1), [5375, 4], "and the grid starts again from there");
}

// ── the transport, static mode: the real ctx and a fake twin ─────────────────────────
function staticRig({ rand = () => 0 } = {}) {
  const c = fakeClock(), toasts = [], twin = fakeTwin();
  const ctx = createCtx({ schema, twin, toast: (m) => toasts.push(m) });
  const heard = [];
  ctx.on("note", (e) => heard.push([c.now(), e.note, e.on, e.velocity]));
  const tr = T.createTransport(ctx, { clock: c, rand });
  const events = [];
  tr.on((st, what) => events.push([what, st]));
  return { c, ctx, tr, twin, heard, toasts, events };
}
{
  const r = staticRig();
  const st0 = r.tr.state;
  eq(st0, { playing: false, paused: false, position: -1, steps: 16, bpm: 120, step_resolution: "1/16" }, "the state starts stopped");
  st0.playing = true;
  ok(r.tr.state.playing === false, "state is a copy: writing to it changes nothing");
  r.tr.play();
  ok(r.toasts.at(-1) === "The sequence is empty. Add a note in the Sequencer first." && !r.tr.state.playing, "an empty sequence does not start, and the toast says what to do");
  ok(r.twin.calls.some((x) => x[0] === "resume"), "Play wakes the twin's audio (inside the key press)");

  r.tr.seq.notes.push({ step: 0, pitch: 60, velocity: 90, duration: 2 }, { step: 2, pitch: 64, velocity: 70, duration: 1 });
  r.tr.edited();
  ok(r.events.at(-1)[0] === "poly" && r.tr.poly.length === 0, "an edit re-counts the four-note warning (static)");
  r.tr.setPerf({ gate: 0.5 });
  r.tr.play();
  eq(r.tr.state, { playing: true, paused: false, position: 0, steps: 16, bpm: 120, step_resolution: "1/16" }, "Play: running from step 1");
  eq(r.heard[0], [0, 60, true, 90], "step 1 sounds C4 through ctx.note, so the keys light");
  ok(r.twin.calls.some((x) => x[0] === "on" && x[1] === 60 && x[2] === 90), "and the twin plays it");
  r.c.advance(125);
  eq(r.heard.slice(1, 2), [[125, 60, false, 100]], "hold = max(0.05, 2 × 0.5) steps = 125 ms");
  r.c.advance(125);
  eq(r.heard.at(-1), [250, 64, true, 70], "step 3 sounds E4");
  ok(r.events.filter(([w]) => w === "position").map(([, s]) => s.position).join() === "0,1,2", "every step ticks the listeners");

  r.tr.toggle();                                      // pause at step 3, E4 still held
  eq(r.tr.state, { playing: false, paused: true, position: 2, steps: 16, bpm: 120, step_resolution: "1/16" }, "toggle while running pauses, at step 3");
  eq(r.heard.at(-1), [250, 64, false, 100], "pausing releases the held note at once");
  const quiet = r.events.length;
  r.c.advance(1000);
  ok(r.events.length === quiet, "paused: no ticks");
  r.tr.toggle();
  ok(r.tr.state.playing && r.events.at(-1)[1].position === 3, "toggle while paused resumes at step 4, not step 1");
  r.tr.stop();
  eq(r.tr.state, { playing: false, paused: false, position: -1, steps: 16, bpm: 120, step_resolution: "1/16" }, "stop: back before step 1");
  r.c.advance(1000);
  ok(r.tr.state.position === -1, "stopped stays stopped");
  r.tr.toggle();
  ok(r.tr.state.position === 0, "Play after a stop starts at step 1");
  r.tr.stop();

  const off = r.tr.on(() => { throw new Error("listener bug"); });
  const saved = console.error, errors = [];
  console.error = (e) => errors.push(e);
  r.tr.play();
  console.error = saved;
  off();
  ok(errors.length >= 1 && r.tr.state.playing, "a throwing listener is reported and the transport keeps going");
  r.tr.stop();
}
{
  const r = staticRig({ rand: () => 0.99 });
  r.tr.seq.notes.push({ step: 0, pitch: 60, velocity: 90, duration: 1 });
  r.tr.setPerf({ probability: 0.5 });
  r.tr.play();
  r.c.advance(500);
  ok(r.heard.length === 0 && r.tr.state.position === 4, "a failed probability roll: the steps tick, nothing sounds");
}
{
  const r = staticRig();
  r.tr.seq.notes.push({ step: 0, pitch: 60, velocity: 90, duration: 4 }, { step: 2, pitch: 60, velocity: 80, duration: 1 });
  r.tr.play();
  r.c.advance(400);
  ok(!r.heard.some(([t, , on]) => !on && t < 500), "a later, shorter note of the same pitch does not cut the long one short");
  r.c.advance(100);
  eq(r.heard.filter(([, , on]) => !on), [[500, 60, false, 100]], "the pitch ends when the last hold ends (the app's rule)");
}
{
  const r = staticRig();
  r.tr.seq.notes.push({ step: 0, pitch: 60, velocity: 90, duration: 1 });
  ok(r.tr.nudgeTempo(1) === 121 && r.tr.seq.bpm === 121 && r.events.at(-2)[0] === "tempo", "= raises the tempo by 1 and tells the listeners");
  ok(r.tr.nudgeTempo(-10) === 111, "⇧ − lowers it by 10");
  ok(r.tr.setTempo(5) === T.TEMPO_MIN && r.tr.setTempo(900) === T.TEMPO_MAX, "the tempo stays within 20 to 300 BPM");
  r.tr.seq.notes.push({ step: 10, pitch: 62, velocity: 90, duration: 6 });
  ok(r.tr.setSteps(12) === 12 && r.tr.seq.notes.find((n) => n.step === 10).duration === 2, "a shorter pattern clamps the notes that cross its end");
  ok(r.tr.setSteps(8) === 8 && r.tr.seq.notes.length === 1, "and drops the notes past its end, as the app does");
  ok(r.tr.setSteps(999) === T.MAX_STEPS, "at most 64 steps");
}

// ── the transport, server mode: a fake app behind ctx.server, messages through ctx.on("server") ──
function serverRig() {
  const c = fakeClock(), toasts = [], twin = fakeTwin(), sent = [], calls = [];
  const app = {
    transport: { playing: false, paused: false, position: 0, bpm: 100, steps: 8 },
    perf: { gate: 0.8, shuffle: 0.25, probability: 1, clock_enabled: false },
    seq: { steps: 8, bpm: 100, step_resolution: "1/8", notes: [{ step: 0, pitch: 48, velocity: 100, duration: 1 }] },
    slow: false, gates: [],
  };
  const api = async (method, path, body) => {
    calls.push([method, path, body ? JSON.parse(JSON.stringify(body)) : body]);
    if (path === "/api/sequence" && method === "GET") return { ...app.seq, poly_warnings: [], transport: { ...app.transport } };
    if (path === "/api/sequence" && method === "PUT") return { ...body, poly_warnings: [3] };
    if (path === "/api/transport" && method === "PUT") return { ...app.transport, ...app.perf };
    if (path === "/api/transport" && method === "POST") {
      if (app.slow) await new Promise((res) => app.gates.push(res));
      const t = app.transport;
      if (body.action === "play") Object.assign(t, t.paused ? { paused: false, playing: true } : { playing: true, paused: false, position: 0 });
      else if (body.action === "pause") Object.assign(t, { playing: false, paused: true });
      else Object.assign(t, { playing: false, paused: false, position: 0 });
      return { ...t };
    }
    throw new Error(`unexpected ${method} ${path}`);
  };
  const upstream = { sendParam: () => {}, sendNote: (n, on, vel) => sent.push(["note", n, on, vel]) };
  const ctx = createCtx({ schema, twin, transport: upstream, server: { api, ws() {} }, toast: (m) => toasts.push(m) });
  const heard = [];
  ctx.on("note", (e) => heard.push([e.note, e.on, e.sound]));
  const tr = T.createTransport(ctx, { clock: c, rand: () => 0 });
  const events = [];
  tr.on((st, what) => events.push([what, st]));
  const say = (m) => ctx._emit("server", m);
  return { c, ctx, tr, twin, sent, calls, app, heard, toasts, events, say };
}
{
  const r = serverRig();
  eq(r.calls.map(([m, p]) => `${m} ${p}`), ["GET /api/sequence", "PUT /api/transport"], "at boot it reads the sequence and the performance settings");
  eq(r.calls[1][2], {}, "an empty PUT (it only reads)");
  await flush();
  eq({ ...r.tr.seq }, { steps: 8, bpm: 100, step_resolution: "1/8", notes: [{ step: 0, pitch: 48, velocity: 100, duration: 1 }] }, "the app's sequence is the one copy");
  eq({ ...r.tr.perf }, { gate: 0.8, shuffle: 0.25, probability: 1, clock: false }, "the app's performance settings");
  ok(r.events.some(([w]) => w === "sequence") && r.events.some(([w]) => w === "perf"), "listeners hear both");

  await r.tr.play();
  eq(r.calls.at(-1), ["POST", "/api/transport", { action: "play" }], "Play goes to the app");
  ok(r.tr.state.playing && r.tr.state.position === 0, "and the app's answer sets the state");
  ok(r.twin.calls.some((x) => x[0] === "resume"), "Play wakes the twin (it may be the sound)");

  r.say({ type: "position", step: 0 });
  eq(r.heard.at(-1), [48, true, "twin"], "no S-1: each step the app plays sounds on the twin, through ctx.note");
  ok(r.twin.calls.some((x) => x[0] === "on" && x[1] === 48) && r.sent.length === 0, "on the twin, nothing to the S-1");
  r.c.advance(Math.round(T.stepSeconds(100, "1/8") * 0.8 * 1000));
  eq(r.heard.at(-1), [48, false, "twin"], "released after duration × gate steps");

  r.ctx._setStatus({ sync: "listening", port: "S-1" });
  const before = r.twin.calls.length;
  r.say({ type: "position", step: 0 });
  ok(r.twin.calls.length === before && r.sent.length === 0 && r.tr.state.position === 0, "the S-1 sounds: the app plays it, the twin stays silent, the playhead still moves");
  r.ctx._setStatus({ sync: "disconnected", port: null });

  r.say({ type: "transport", playing: false, paused: true, position: 3, bpm: 100, steps: 8 });
  ok(r.tr.state.paused && !r.tr.state.playing && r.tr.state.position === 3, "a transport message from the app sets the state (paused at step 4)");
  r.say({ type: "position", step: 4 });
  ok(r.tr.state.paused && r.tr.state.position === 3, "a tick sent before the pause landed neither restarts nor sounds");
  r.say({ type: "transport", playing: false, paused: false, position: 0, bpm: 100, steps: 8 });
  r.say({ type: "position", step: 5 });
  ok(!r.tr.state.playing && r.tr.state.position === -1, "nor after a stop");

  r.say({ type: "hello", sequence: { steps: 4, bpm: 90, step_resolution: "1/16", notes: [] }, status: { transport: { playing: true, paused: false, position: 2 } } });
  ok(r.tr.seq.steps === 4 && r.tr.seq.bpm === 90 && r.tr.seq.notes.length === 0, "hello: the app's sequence");
  ok(r.tr.state.playing && r.tr.state.position === 2, "hello: the app's transport (it was already playing)");
  r.say({ type: "position", step: 3 });
  ok(r.tr.state.position === 3 && r.events.at(-1)[0] === "position", "ticks while the app plays");
}
{
  const r = serverRig();
  await flush();
  const n0 = r.calls.length;
  r.say({ type: "sequence" });
  await flush();
  eq(r.calls.slice(n0).map(([m, p]) => `${m} ${p}`), ["GET /api/sequence"], "another tab changed the sequence: read it");

  r.tr.seq.notes.push({ step: 1, pitch: 50, velocity: 90, duration: 1 });
  r.tr.edited();
  r.say({ type: "sequence" });
  await flush();
  ok(r.calls.length === n0 + 1, "an edit is queued: the app's echo does not overwrite it");
  r.c.advance(179);
  ok(r.calls.length === n0 + 1, "edits wait a moment (a drag sends one request)");
  r.c.advance(1);
  eq(r.calls.at(-1), ["PUT", "/api/sequence", T.sequencePayload(r.tr.seq)], "then the sequence goes to the app");
  await flush();
  ok(r.tr.poly.join() === "3" && r.events.at(-1)[0] === "poly", "the app's four-note warning comes back");

  r.tr.hold(true);
  r.say({ type: "sequence" });
  await flush();
  ok(r.calls.length === n0 + 2, "while a drag holds the notes, echoes wait");
  r.tr.hold(false);
  r.tr.edited({ now: true });
  eq(r.calls.at(-1)[0], "PUT", "edited({now}) sends at once");

  r.tr.setPerf({ gate: 0.5 });
  r.tr.setPerf({ shuffle: 0.1 });
  r.c.advance(120);
  eq(r.calls.at(-1), ["PUT", "/api/transport", { gate: 0.5, shuffle: 0.1, probability: 1 }], "knob turns go up together, once");
  r.tr.setPerf({ clock: true });
  eq(r.calls.at(-1), ["PUT", "/api/transport", { clock_enabled: true }], "the MIDI clock switch goes at once");

  r.tr.nudgeTempo(10);
  r.c.advance(180);
  ok(r.calls.at(-1)[1] === "/api/sequence" && r.calls.at(-1)[2].bpm === 110, "a tempo key sends the new tempo with the sequence");
}
{
  const r = serverRig();
  await flush();
  r.app.slow = true;
  const n0 = r.calls.length;
  r.tr.toggle();                                     // play, on its way
  r.tr.toggle();                                     // a quick second press: pause
  await flush();
  eq(r.calls.slice(n0).map(([, , b]) => b.action), ["play"], "one request at a time: the pause waits for the play's answer");
  r.app.gates.shift()();
  await flush();
  eq(r.calls.slice(n0).map(([, , b]) => b.action), ["play", "pause"], "then the pause goes");
  r.app.gates.shift()();
  await flush();
  ok(r.tr.state.paused && !r.tr.state.playing, "two quick presses: play, then pause");
  r.app.slow = false;
  await r.tr.stop();
  ok(r.calls.at(-1)[2].action === "stop" && r.tr.state.position === -1, "stop goes to the app and the playhead leaves");
  r.tr.seq.notes = [];
  const n1 = r.calls.length;
  r.tr.toggle();
  ok(r.calls.length === n1 && r.toasts.at(-1) === "The sequence is empty. Add a note in the Sequencer first.", "an empty sequence is not sent (the app would stop at once)");
  r.tr.destroy();
  const n2 = r.events.length;
  r.say({ type: "transport", playing: true, paused: false, position: 1 });
  ok(r.events.length === n2, "destroy() stops following the app");
}

console.log(`transport: ${checks} checks passed`);
