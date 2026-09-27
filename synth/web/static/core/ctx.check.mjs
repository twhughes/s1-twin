// node synth/web/static/core/ctx.check.mjs — exit 0 = the context keeps its contract (BUILD.md §2.2):
// set/on/source semantics, upstream sends, note routing, status words, and the echo filter.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

import { createCtx, syncOf } from "./ctx.js";
import { createEchoFilter } from "./server.js";

const here = dirname(fileURLToPath(import.meta.url));
const schema = JSON.parse(readFileSync(join(here, "schema.json"), "utf8"));
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };

function fakeTwin() {
  const calls = [];
  return {
    calls,
    set: (cc, v) => calls.push(["set", cc, v]),
    setAll: (m) => calls.push(["setAll", Object.keys(m).length]),
    noteOn: (n, v) => calls.push(["on", n, v]),
    noteOff: (n) => calls.push(["off", n]),
    allOff: () => calls.push(["allOff"]),
    resume: () => calls.push(["resume"]),
    modeled: () => true,
  };
}
function fakeTransport() {
  const sent = [];
  return { sent, sendParam: (cc, v) => sent.push(["param", cc, v]), sendNote: (n, on, vel) => sent.push(["note", n, on, vel]) };
}
function make({ transport = fakeTransport(), twin = fakeTwin() } = {}) {
  const ctx = createCtx({ schema, twin, transport, server: transport ? { api() {}, ws() {} } : null });
  const events = { param: [], status: [], note: [] };
  for (const k of Object.keys(events)) ctx.on(k, (e) => events[k].push(e));
  return { ctx, twin, transport, events };
}

// ── boot: params from the schema, the twin mirrors them ───────────────────
{
  const { ctx, twin } = make();
  ok(ctx.params instanceof Map && ctx.params.size === 54, "params is a Map of all 54 CCs");
  ok(ctx.params.get(74) === 127 && ctx.params.get(12) === 2, "params start at the schema's values");
  eq(twin.calls[0], ["setAll", 54], "the twin gets the whole state at boot");
  ok(ctx.schema === schema && typeof ctx.toast === "function", "schema and toast are on ctx");
  const seeded = createCtx({ schema, twin: fakeTwin(), values: { 74: 12, 12: 99 } });
  ok(seeded.params.get(74) === 12 && seeded.params.get(12) === 5, "seed values are clamped to the schema range");
}

// ── set: update, notify with the source, mirror to the twin, send upstream ─
{
  const { ctx, twin, transport, events } = make();
  ok(ctx.set(74, 90) === true, "a change returns true");
  eq(events.param.at(-1), { cc: 74, value: 90, source: "ui" }, "default source is ui");
  eq(transport.sent.at(-1), ["param", 74, 90], "ui changes go upstream");
  eq(twin.calls.at(-1), ["set", 74, 90], "the twin follows every change");
  ok(ctx.params.get(74) === 90, "params holds the new value");

  const n = events.param.length, s = transport.sent.length;
  ok(ctx.set(74, 90) === false && events.param.length === n && transport.sent.length === s, "an unchanged value is silent");

  ctx.set(12, 99);
  ok(ctx.params.get(12) === 5 && events.param.at(-1).value === 5, "values clamp to the parameter's range");
  ctx.set(71, 10.6);
  ok(ctx.params.get(71) === 11, "values round to integers");
  ok(ctx.set(9, 10) === false && !ctx.params.has(9), "an unknown CC is ignored");

  ctx.set(71, 40, { source: "midi" });
  eq(events.param.at(-1), { cc: 71, value: 40, source: "midi" }, "midi keeps its source");
  ok(transport.sent.at(-1)[1] !== 71 || transport.sent.at(-1)[2] !== 40, "midi is never sent back upstream");
  eq(twin.calls.at(-1), ["set", 71, 40], "the twin follows midi too");

  for (const source of ["patch", "match"]) {
    ctx.set(24, source === "patch" ? 30 : 31, { source });
    eq(events.param.at(-1).source, source, `${source} keeps its source`);
    eq(transport.sent.at(-1), ["param", 24, source === "patch" ? 30 : 31], `${source} goes upstream`);
  }
}

// ── on() returns off(); a throwing listener does not starve the others ─────
{
  const { ctx } = make();
  const got = [];
  const off = ctx.on("param", (e) => got.push(e.value));
  ctx.set(74, 1);
  off();
  ctx.set(74, 2);
  eq(got, [1], "off() unsubscribes");
  const errors = [];
  const saved = console.error;
  console.error = (e) => errors.push(e);
  ctx.on("param", () => { throw new Error("listener bug"); });
  const after = [];
  ctx.on("param", (e) => after.push(e.value));
  ctx.set(74, 3);
  console.error = saved;
  ok(after[0] === 3 && errors.length === 1, "a throwing listener is reported and the next one still runs");
}

// ── values from upstream: applied and notified, never echoed back ─────────
{
  const { ctx, transport, events } = make();
  ctx._receive(74, 55, "midi");
  eq(events.param.at(-1), { cc: 74, value: 55, source: "midi" }, "a received knob twist notifies as midi");
  ctx._receive(71, 66, "patch");
  ctx._receiveAll({ 74: 60, 71: 66, 5: 10 }, "patch");
  ok(ctx.params.get(74) === 60 && ctx.params.get(5) === 10, "a hello applies every value (string keys too)");
  ok(transport.sent.length === 0, "nothing received is sent back");
}

// ── status words and note routing ──────────────────────────────────────────
{
  ok(syncOf("synced") === "synced" && syncOf("listening") === "pending" && syncOf("connecting") === "pending"
    && syncOf("disconnected") === "offline" && syncOf(undefined) === "offline", "server words map to three");
  const { ctx, twin, transport, events } = make();
  ok(ctx.soundSource === "twin" && ctx.status.sync === "offline", "no S-1: the twin sounds");
  ctx.note(45, true, 90);
  eq(twin.calls.slice(-2), [["resume"], ["on", 45, 90]], "offline notes play the twin (resumed first)");
  eq(events.note.at(-1), { note: 45, on: true, velocity: 90, sound: "twin" }, "every note is announced");
  ctx.note(45, false);
  eq(twin.calls.at(-1), ["off", 45], "and released on the twin");

  ctx._setStatus({ sync: "connecting", port: null });
  ok(ctx.status.sync === "pending" && ctx.soundSource === "twin", "connecting: still the twin (no port yet)");
  ctx._setStatus({ sync: "listening", port: "S-1 MIDI IN", keyboards: ["Keystation"], mode: "solo" });
  ok(ctx.status.sync === "pending" && ctx.status.link === "listening" && ctx.soundSource === "s1", "listening: the S-1 sounds");
  eq(events.status.at(-1).keyboards, ["Keystation"], "status carries keyboards");
  ctx.note(60, true);
  eq(transport.sent.at(-1), ["note", 60, true, 100], "notes go to the S-1 with velocity 100 by default");
  ctx._setStatus({ sync: "synced" });
  ok(ctx.status.sync === "synced" && ctx.status.port === "S-1 MIDI IN", "synced keeps the port");
  ctx.note(60, false);
  eq(transport.sent.at(-1), ["note", 60, false, 100], "the note-off follows its note-on to the S-1");

  ctx.note(62, true);
  ctx._setStatus({ sync: "disconnected", port: null });
  eq(transport.sent.at(-1), ["note", 62, false, 64], "unplugging releases what the S-1 still holds");
  eq(events.note.at(-1), { note: 62, on: false, velocity: 64, sound: "s1" }, "and clears its halo");
  ctx.note(64, true);
  ctx._setStatus({ sync: "listening" });
  ok(twin.calls.some((c) => c[0] === "allOff"), "plugging in silences the twin");
  const sentBefore = transport.sent.length;
  ctx.note(64, false);
  ok(twin.calls.at(-1)[0] === "allOff" && transport.sent.length === sentBefore,
    "a key-up after the switch reaches neither the twin nor the S-1 (the switch released it)");
  eq(events.note.at(-1), { note: 64, on: false, velocity: 100, sound: "s1" }, "its note-off is still announced");

  ctx._noteSeen(50, true, 70);
  ok(events.note.at(-1).seen === true && transport.sent.at(-1)[1] !== 50, "a seen note is shown, not sent");
}

// ── static mode: no transport, so the twin always sounds ──────────────────
{
  const { ctx, twin, events } = make({ transport: null });
  ctx.set(74, 20);
  ok(ctx.params.get(74) === 20 && events.param.length === 1, "static set works with no server");
  ctx._setStatus({ sync: "synced" });
  ok(ctx.soundSource === "twin", "with no transport the twin is the only sound");
  ctx.note(48, true);
  eq(twin.calls.at(-1), ["on", 48, 100], "static notes play the twin");
}

// ── the echo filter ────────────────────────────────────────────────────────
{
  let t = 0;
  const echo = createEchoFilter({ windowMs: 1000, now: () => t });
  echo.sent(74, 50); echo.sent(74, 51); echo.sent(74, 52);
  ok(echo.isEcho(74, 50) && echo.isEcho(74, 51), "our own sends come back as echoes");
  ok(!echo.isEcho(74, 60), "another tab's value is not an echo");
  ok(echo.isEcho(74, 52) && !echo.isEcho(74, 52), "each send is consumed once");
  echo.sent(71, 10); echo.sent(71, 20);
  ok(echo.isEcho(71, 20) && !echo.isEcho(71, 10), "a newer echo consumes older ones");
  echo.sent(5, 1);
  t = 5000;
  ok(!echo.isEcho(5, 1), "sends expire after the window");
  ok(!echo.isEcho(99, 1), "a CC we never sent is never an echo");
}

console.log(`ctx: ${checks} checks passed`);
