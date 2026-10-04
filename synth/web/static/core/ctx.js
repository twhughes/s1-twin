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
//   ctx.voices            how many notes the twin sounds at once: the S-1's 4 while an S-1 is linked,
//                         else the choice (VOICE_CHOICES). ctx.voiceChoice, ctx.voicesLocked,
//                         ctx.setVoices(n), ctx.on("voices", fn) fn({voices, choice, locked})
//   ctx.keys              the keyboard service (core/keys.js), attached by the shell
//   ctx.transport         the sequence's transport (core/transport.js), attached by the shell
//   ctx.shortcuts         the keyboard shortcuts (core/shortcuts.js), attached by the shell
//
// Status words. The server's sync states map onto three: "synced" (Send patch to S-1 has run: the
// S-1 and the plate agree), "pending" (the S-1 is plugged in and listened to, knob moves flow both
// ways, but its patch may differ until the patch is sent; or it is still connecting), "offline".
// Notes follow the sound you hear: while the S-1's port is open ("listening" or "synced") they go
// to the hardware, otherwise to the twin. `status.link` keeps the server's own word.
//
// Voices. The S-1 plays 4 notes at once. In the browser the twin may play 8 or 16 (the choice;
// default 8 on the static page, 4 in the cockpit), but while an S-1 is linked (its port open, or the
// connected demo) the twin stands in for it and plays 4, whatever the choice.

import { allParams } from "./layout.js";

const SYNC_OF_LINK = { synced: "synced", listening: "pending", connecting: "pending", disconnected: "offline" };
const PORT_OPEN = new Set(["listening", "synced"]);

/** The S-1's own polyphony: the twin's voice count while an S-1 is linked. */
export const S1_VOICES = 4;
/** The voice counts the page offers (twin/dsp.js VOICE_COUNTS; twin/voices.check.mjs keeps them equal). */
export const VOICE_CHOICES = [4, 8, 16];
/** Where the page keeps the choice (localStorage; app.js reads it, the Settings drawer writes it). */
export const VOICES_KEY = "synth.twinVoices";
/** The choice before anyone makes one: 8 on the static page, the S-1's 4 in the cockpit. */
export const defaultVoices = (server) => (server ? S1_VOICES : 8);

/** The three-word sync state for a server link state. */
export const syncOf = (link) => SYNC_OF_LINK[link] || "offline";

export function createCtx({ schema, values = null, twin, transport = null, server = null, toast = () => {}, voices = null }) {
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

  let voiceChoice = VOICE_CHOICES.includes(Number(voices)) ? Number(voices) : defaultVoices(server);
  let shown = { voices: 0, choice: 0, locked: null };   // what the twin and the "voices" listeners last got
  const linked = () => PORT_OPEN.has(status.link);
  /** Give the twin the count that applies now, and tell the listeners when anything changed. */
  function applyVoices() {
    const locked = linked();
    const now = { voices: locked ? S1_VOICES : voiceChoice, choice: voiceChoice, locked };
    if (now.voices !== shown.voices) {
      try { twin.setVoices?.(now.voices); } catch (e) { console.error(e); }
    }
    const changed = now.voices !== shown.voices || now.choice !== shown.choice || now.locked !== shown.locked;
    shown = now;
    if (changed) emit("voices", { ...now });
  }

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
    /** How many notes the twin sounds at once now: the S-1's 4 while one is linked, else the choice. */
    get voices() { return shown.voices; },
    /** The count chosen for this browser (4, 8 or 16). It applies whenever no S-1 is linked. */
    get voiceChoice() { return voiceChoice; },
    /** True while an S-1 is linked: the twin plays the S-1's 4 voices, whatever the choice. */
    get voicesLocked() { return linked(); },
    /** Choose 4, 8 or 16 voices. Returns true when the choice changed (anything else is ignored). */
    setVoices(n) {
      const v = Number(n);
      if (!VOICE_CHOICES.includes(v) || v === voiceChoice) return false;
      voiceChoice = v;
      applyVoices();
      return true;
    },

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
      applyVoices();
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
  applyVoices();
  return ctx;
}
