// node core/opener-sync.check.mjs — the music app's sync (core/opener-sync.js) over a fake ctx and window.
import assert from "node:assert/strict";
import { startOpenerSync } from "./opener-sync.js";
import { readHash } from "./flags.js";

let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks += 1; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks += 1; };

function fakeCtx(values) {
  const params = new Map(Object.entries(values).map(([cc, v]) => [Number(cc), v]));
  const fns = new Set();
  return {
    params,
    on(evt, fn) { if (evt === "param") fns.add(fn); return () => fns.delete(fn); },
    turn(cc, value, source = "ui") { params.set(cc, value); for (const fn of [...fns]) fn({ cc, value, source }); },
    listeners: () => fns.size,
  };
}
function fakeOpener() {
  const got = [];
  return { got, closed: false, postMessage(msg, origin) { got.push({ msg, origin }); } };
}
const flags = (hash) => readHash(hash).flags;

// 1. Opened by the music app: the whole set first, then every change, to the opener.
{
  const ctx = fakeCtx({ 74: 70, 71: 12 });
  const opener = fakeOpener();
  const sync = startOpenerSync(ctx, { flags: flags("#sync=music"), win: { opener } });
  ok(sync !== null, "#sync=music with an opener starts the sync");
  eq(opener.got[0], { msg: { v: 1, type: "s1-twin:values", values: { 74: 70, 71: 12 } }, origin: "*" },
    "the whole set goes first");
  ctx.turn(74, 90);
  ctx.turn(20, 0, "midi");
  eq(opener.got.slice(1).map((g) => g.msg), [
    { v: 1, type: "s1-twin:param", cc: 74, value: 90 },
    { v: 1, type: "s1-twin:param", cc: 20, value: 0 },
  ], "then each change, from any source");
  sync.stop();
  ctx.turn(74, 10);
  eq(opener.got.length, 3, "stop() ends it");
  eq(ctx.listeners(), 0, "and lets go of the ctx");
}

// 2. A view in the hash still syncs: "#synth&sync=music".
{
  const opener = fakeOpener();
  ok(startOpenerSync(fakeCtx({ 74: 70 }), { flags: flags("#synth&sync=music"), win: { opener } }) !== null,
    "the flag sits beside a view");
}

// 3. Anything else: no sync, nothing sent.
{
  const opener = fakeOpener();
  ok(startOpenerSync(fakeCtx({ 74: 70 }), { flags: flags("#synth"), win: { opener } }) === null, "no flag, no sync");
  ok(startOpenerSync(fakeCtx({ 74: 70 }), { flags: flags("#sync=other"), win: { opener } }) === null, "another peer, no sync");
  ok(startOpenerSync(fakeCtx({ 74: 70 }), { flags: flags("#sync=music"), win: { opener: null } }) === null,
    "no opener (a bookmarked link), no sync");
  eq(opener.got.length, 0, "and nothing was sent");
}

// 4. The music page closed or went away: sending stops quietly, nothing throws.
{
  const ctx = fakeCtx({ 74: 70 });
  const opener = fakeOpener();
  startOpenerSync(ctx, { flags: flags("#sync=music"), win: { opener } });
  opener.closed = true;
  ctx.turn(74, 1);
  eq(opener.got.length, 1, "a closed opener gets nothing more");
  const gone = { get closed() { throw new Error("cross-origin"); }, postMessage() { throw new Error("gone"); } };
  const ctx2 = fakeCtx({ 74: 70 });
  startOpenerSync(ctx2, { flags: flags("#sync=music"), win: { opener: gone } });
  ctx2.turn(74, 2);
  ok(true, "an opener that throws is ignored");
}

console.log(`opener-sync.check.mjs: ${checks} checks passed`);
