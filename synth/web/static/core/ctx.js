// core/ctx.js — the app context (docs/design/BUILD.md §2.2). W-plate owns it; every view builds on it.
// Pure (no DOM): `node core/ctx.check.mjs` drives it with a fake transport and a fake twin.
//
//   ctx.schema                  /api/schema JSON (server) or core/schema.json (static)
//   ctx.params                  Map cc -> value (0..127): the single client-side source of truth
//   ctx.set(cc, v, {source})    source "ui" | "midi" | "patch" | "match": updates params, notifies,
//                               sends upstream (every source but "midi", which came from upstream)
//   ctx.on("param", fn)         fn({cc, value, source}) for every change from any source
//   ctx.on("status", fn)        fn(status): {sync: "synced"|"pending"|"offline", port, monitor, keyboards, mode, …}
//   ctx.note(n, on, vel = 100)  to the S-1 while its MIDI port is open, else to the browser twin
//   ctx.twin                    the browser twin (§2.3), always present
//   ctx.server                  null in static mode; else {api(method, path, body), ws(path) -> WebSocket}
//   ctx.toast(msg)
//   ctx.soundSource             "s1" | "twin"
//
// Additive extras (views may use them; nothing in §2.2 changes):
//   ctx.on(…) returns an off() function     ctx.status   the latest status object
//   ctx.on("note", fn)    fn({note, on, velocity, sound}) for every note from any key source
//   ctx.on("server", fn)  fn(msg) for every raw /ws/state message (transport, position, sequence, …)
//   ctx.keys              the keyboard service (core/keys.js), attached by the shell
//
// Status words. The server's sync states map onto three: "synced" (Send patch to S-1 has run: the
// S-1 and the plate agree), "pending" (the S-1 is plugged in and listened to, knob moves flow both
// ways, but its patch may differ until the patch is sent; or it is still connecting), "offline".
// Notes follow the sound you hear: while the S-1's port is open ("listening" or "synced") they go
// to the hardware, otherwise to the twin. `status.link` keeps the server's own word.

import { allParams } from "./layout.js";

const SYNC_OF_LINK = { synced: "synced", listening: "pending", connecting: "pending", disconnected: "offline" };
const PORT_OPEN = new Set(["listening", "synced"]);

/** The three-word sync state for a server link state. */
export const syncOf = (link) => SYNC_OF_LINK[link] || "offline";

export function createCtx({ schema, values = null, twin, transport = null, server = null, toast = () => {} }) {
  const range = new Map(allParams(schema).map((p) => [p.cc, [p.min, p.max]]));
  const clamp = (cc, v) => {
    const [lo, hi] = range.get(cc) || [0, 127];
    return Math.max(lo, Math.min(hi, Math.round(Number(v))));
  };
  const params = new Map();
  for (const p of allParams(schema)) params.set(p.cc, clamp(p.cc, values?.[p.cc] ?? p.value ?? p.default));

  const listeners = new Map();
  const emit = (evt, payload) => {
    for (const fn of [...(listeners.get(evt) || [])]) {
      try { fn(payload); } catch (e) { console.error(e); }
    }
  };

  let status = { sync: "offline", link: "disconnected", port: null, monitor: null, keyboards: [], mode: "solo" };
  const held = new Map();                    // note -> where its note-on went ("s1" | "twin")
  const soundOf = (st) => (PORT_OPEN.has(st.link) && transport ? "s1" : "twin");

  function apply(cc, v, source, upstream) {
    if (!range.has(cc)) return false;
    const value = clamp(cc, v);
    if (!Number.isFinite(value) || params.get(cc) === value) return false;
    params.set(cc, value);
    try { twin.set(cc, value); } catch (e) { console.error(e); }
    emit("param", { cc, value, source });
    if (upstream && transport) transport.sendParam(cc, value);
    return true;
  }

  const ctx = {
    schema,
    params,
    twin,
    server,
    toast,
    keys: null,
    get soundSource() { return soundOf(status); },
    get status() { return status; },

    set(cc, v, { source = "ui" } = {}) {
      return apply(Number(cc), v, source, source !== "midi");
    },

    on(evt, fn) {
      if (!listeners.has(evt)) listeners.set(evt, new Set());
      listeners.get(evt).add(fn);
      return () => listeners.get(evt)?.delete(fn);
    },

    note(n, on, vel = 100) {
      const note = Math.max(0, Math.min(127, Math.round(n)));
      const velocity = Math.max(1, Math.min(127, Math.round(vel)));
      let sound = ctx.soundSource;
      if (on) {
        if (sound === "s1") transport.sendNote(note, true, velocity);
        else { twin.resume?.(); twin.noteOn(note, velocity); }
        held.set(note, sound);
      } else {
        const was = held.get(note);           // the note-off goes where the note-on went;
        held.delete(note);                    // a note nobody holds (released on a switch) goes nowhere
        if (was) sound = was;
        if (was === "s1" && transport) transport.sendNote(note, false, velocity);
        else if (was === "twin") twin.noteOff(note);
      }
      emit("note", { note, on: Boolean(on), velocity, sound });
    },

    // ── the shell's side (views do not call these) ─────────────────────────
    /** A value that arrived from upstream (the server): apply and notify, never echo back. */
    _receive(cc, v, source = "ui") { return apply(Number(cc), v, source, false); },
    /** A whole state (the server's hello): {cc: value} with string or number keys. */
    _receiveAll(values, source = "patch") {
      for (const [cc, v] of Object.entries(values || {})) apply(Number(cc), v, source, false);
    },
    /** Merge a status patch. A `sync` field is the server's word ("listening", …): it becomes
     *  `link`, and `sync` becomes the three-word state. Fires "status" with the new object. */
    _setStatus(patch) {
      const before = soundOf(status);
      const next = { ...status, ...patch };
      if ("sync" in patch) next.link = patch.sync;
      next.sync = syncOf(next.link);
      status = next;
      if (before !== soundOf(status)) releaseAll(before);
      emit("status", status);
    },
    /** A note that reached the S-1 by another path (a hardware keyboard): show it, send nothing. */
    _noteSeen(n, on, vel = 100) {
      emit("note", { note: n, on: Boolean(on), velocity: vel, sound: "s1", seen: true });
    },
    _emit: emit,
  };

  /** The sound source changed: silence what the old one still holds, clear every halo. */
  function releaseAll(was) {
    if (was === "twin") { try { twin.allOff(); } catch (e) { console.error(e); } }
    for (const note of [...held.keys()]) {
      if (held.get(note) === "s1" && transport) transport.sendNote(note, false, 64);
      held.delete(note);
      emit("note", { note, on: false, velocity: 64, sound: was });
    }
  }

  try { twin.setAll(Object.fromEntries(params)); } catch (e) { console.error(e); }
  return ctx;
}
