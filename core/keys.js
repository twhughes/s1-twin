// core/keys.js — playing the synth: the computer keyboard, the on-screen keyboard, and Web MIDI.
// Every key source calls ctx.note(), which sends to the S-1 while its port is open and to the browser
// twin otherwise. Keys light in their pitch color only while they sound (DIRECTION rule 2).
//
//   const keys = createKeys(ctx)      one per page (the shell attaches it as ctx.keys)
//   keys.qwerty(true)                 A W S E D F T G Y H U J K O L P play; Z and X move the octave
//   const board = keys.mount(el)      an on-screen keyboard (two octaves and a top C); board.destroy()
//   keys.octave(±1), keys.base        the lowest on-screen key (the computer keys start an octave up)
//   keys.velocity                     velocity for computer and screen keys (1..127)
//   keys.midi.enable() -> names       Web MIDI input (asks the browser once); keys.midi.names()
//   keys.onChange(fn)                 octave or MIDI inputs changed

import { rgbOf, lum, noteName } from "../design/colors.js";

export const KEYMAP = {
  KeyA: 0, KeyW: 1, KeyS: 2, KeyE: 3, KeyD: 4, KeyF: 5, KeyT: 6, KeyG: 7,
  KeyY: 8, KeyH: 9, KeyU: 10, KeyJ: 11, KeyK: 12, KeyO: 13, KeyL: 14, KeyP: 15,
};
const LETTERS = Object.keys(KEYMAP).map((code) => code.slice(3));
const BLACK = new Set([1, 3, 6, 8, 10]);
export const isBlack = (n) => BLACK.has(((n % 12) + 12) % 12);
export const SPAN = 25;                       // C to C, two octaves
export const BASE_MIN = 12, BASE_MAX = 96;

/** True while focus is in a place that takes typing (the shortcuts in core/shortcuts.js use it too). */
export const typing = (t) => Boolean(t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)));

export function createKeys(ctx, { base = 36, velocity = 100 } = {}) {
  let qwertyOn = false;
  const down = new Map();                      // KeyboardEvent.code -> note
  const lit = new Set();                       // notes sounding (from any source)
  const boards = new Set();
  const changes = new Set();
  let access = null;
  let inputs = [];

  const changed = () => changes.forEach((fn) => fn());

  // ── lights ──────────────────────────────────────────────────────────────
  function paint(board, n) {
    const el = board.keyEls.get(n);
    if (!el) return;
    const on = lit.has(n);
    if (on) {
      const rgb = rgbOf(n % 12);
      el.style.setProperty("--pc", rgb.join(","));
      el.style.setProperty("--on-pc", lum(rgb) > 0.55 ? "var(--field)" : "var(--ink)");
    }
    el.classList.toggle("down", on);
  }
  const offNote = ctx.on("note", ({ note, on }) => {
    if (on) lit.add(note); else lit.delete(note);
    boards.forEach((b) => paint(b, note));
  });

  // ── the computer keyboard ────────────────────────────────────────────────
  function onKeyDown(e) {
    if (!qwertyOn || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return;
    if (e.code === "KeyZ" || e.code === "KeyX") {
      e.preventDefault();
      if (!e.repeat) api.octave(e.code === "KeyZ" ? -1 : 1);
      return;
    }
    const off = KEYMAP[e.code];
    if (off === undefined) return;
    e.preventDefault();
    if (e.repeat || down.has(e.code)) return;
    const n = base + 12 + off;
    if (n > 127) return;
    down.set(e.code, n);
    ctx.note(n, true, velocity);
  }
  function onKeyUp(e) {
    const n = down.get(e.code);
    if (n === undefined) return;
    down.delete(e.code);
    ctx.note(n, false);
  }
  function releaseQwerty() {
    for (const n of down.values()) ctx.note(n, false);
    down.clear();
  }
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", releaseQwerty);

  // ── on-screen keyboards ─────────────────────────────────────────────────
  function build(board) {
    const { el } = board;
    el.replaceChildren();
    board.keyEls.clear();
    const whites = [];
    for (let n = base; n < base + SPAN; n++) if (!isBlack(n)) whites.push(n);
    const ww = 100 / whites.length;
    for (const n of whites) {
      const k = document.createElement("div");
      k.className = "wk";
      k.dataset.n = n;
      k.setAttribute("aria-label", noteName(n));
      el.appendChild(k);
      board.keyEls.set(n, k);
    }
    for (let n = base; n < base + SPAN; n++) {
      if (!isBlack(n)) continue;
      const left = whites.indexOf(n - 1);
      const k = document.createElement("div");
      k.className = "bk";
      k.dataset.n = n;
      k.setAttribute("aria-label", noteName(n));
      k.style.left = `calc(${(left + 1) * ww}% - ${ww * 0.31}%)`;
      k.style.width = `${ww * 0.62}%`;
      el.appendChild(k);
      board.keyEls.set(n, k);
    }
    LETTERS.forEach((letter, i) => {
      const k = board.keyEls.get(base + 12 + i);
      if (k) k.textContent = letter;
    });
    for (const n of board.keyEls.keys()) paint(board, n);
  }

  function mount(el) {
    const board = { el, keyEls: new Map(), pointer: new Map() };
    el.classList.add("keyboard");
    el.setAttribute("role", "group");
    el.setAttribute("aria-label", "Keyboard");
    const noteAt = (x, y) => {
      const t = document.elementFromPoint(x, y);
      return t && t.dataset && t.dataset.n && el.contains(t) ? Number(t.dataset.n) : null;
    };
    const press = (id, n) => {
      const was = board.pointer.get(id);
      if (was === n) return;
      if (was != null) ctx.note(was, false);
      if (n != null) { board.pointer.set(id, n); ctx.note(n, true, velocity); } else board.pointer.delete(id);
    };
    const onDown = (e) => {
      const n = e.target.dataset ? Number(e.target.dataset.n) : NaN;
      if (!Number.isFinite(n)) return;
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      press(e.pointerId, n);
    };
    const onMove = (e) => { if (board.pointer.has(e.pointerId)) press(e.pointerId, noteAt(e.clientX, e.clientY)); };
    const onUp = (e) => press(e.pointerId, null);
    el.addEventListener("pointerdown", onDown);
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onUp);
    boards.add(board);
    build(board);
    return {
      destroy() {
        for (const id of [...board.pointer.keys()]) press(id, null);
        el.removeEventListener("pointerdown", onDown);
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
        el.removeEventListener("pointercancel", onUp);
        boards.delete(board);
        el.replaceChildren();
      },
    };
  }

  // ── Web MIDI (the browser's own input; the cockpit server forwards keyboards to the S-1 itself) ──
  function route(input, n, on, vel) {
    const fromS1 = /s-1/i.test(input.name || "");
    // While the S-1 sounds, the server already forwards this keyboard: show the note, send nothing.
    // The S-1's own MIDI out never plays the twin (it would double what the S-1 plays).
    if (ctx.soundSource === "twin" && !fromS1) ctx.note(n, on, vel);
    else ctx._noteSeen?.(n, on, vel);
  }
  function bindInputs() {
    inputs = [];
    for (const input of access.inputs.values()) {
      input.onmidimessage = (e) => {
        const [st, d1, d2] = e.data;
        const type = st & 0xf0;
        if (type === 0x90 && d2 > 0) route(input, d1, true, d2);
        else if (type === 0x80 || (type === 0x90 && d2 === 0)) route(input, d1, false, 64);
      };
      inputs.push(input.name);
    }
    changed();
  }
  const midi = {
    supported: typeof navigator !== "undefined" && typeof navigator.requestMIDIAccess === "function",
    get enabled() { return Boolean(access); },
    names: () => [...inputs],
    async enable() {
      if (!midi.supported) throw new Error("This browser has no Web MIDI. Chrome and Edge have it.");
      if (!access) {
        access = await navigator.requestMIDIAccess();
        access.onstatechange = bindInputs;
      }
      bindInputs();
      return [...inputs];
    },
  };
  // A browser that already said yes needs no second ask.
  if (midi.supported && navigator.permissions?.query) {
    navigator.permissions.query({ name: "midi" })
      .then((p) => { if (p.state === "granted") midi.enable().catch(() => {}); })
      .catch(() => {});
  }

  const api = {
    get base() { return base; },
    get velocity() { return velocity; },
    set velocity(v) { velocity = Math.max(1, Math.min(127, Math.round(v))); },
    qwerty(on) { qwertyOn = Boolean(on); if (!qwertyOn) releaseQwerty(); },
    octave(delta) {
      const next = Math.max(BASE_MIN, Math.min(BASE_MAX, base + 12 * delta));
      if (next === base) return;
      base = next;
      boards.forEach(build);
      changed();
    },
    /** "C2 to C4": the on-screen range, for the hint line. */
    range: () => `${noteName(base)} to ${noteName(base + SPAN - 1)}`,
    mount,
    midi,
    onChange(fn) { changes.add(fn); return () => changes.delete(fn); },
    destroy() {
      releaseQwerty();
      offNote();
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", releaseQwerty);
    },
  };
  return api;
}
