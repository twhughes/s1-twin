// node synth/web/static/core/shortcuts.check.mjs — exit 0 = the keyboard shortcuts keep their contract
// (docs/design/ROUND2.md §2): the key -> action table, Shift where the table allows it, never with
// Cmd/Ctrl/Alt or while typing, a key a focused control took is left alone, Space prevents its default
// on keydown AND keyup (so it never clicks a focused button), repeats, view-only keys, the list of keys.
import assert from "node:assert/strict";

import { actionFor, typing, isSpace, createShortcuts, SHEET, VIEW_KEYS } from "./shortcuts.js";

let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b, msg) => { assert.deepEqual(a, b, msg); checks++; };

const key = (k, extra = {}) => ({ key: k, code: extra.code || "", shiftKey: false, metaKey: false, ctrlKey: false, altKey: false, ...extra });
const SPACE = { code: "Space" };

// ── the table ─────────────────────────────────────────────────────────────────────────
for (const view of ["synth", "sequencer", "match"]) {
  eq(actionFor(key(" ", SPACE), { view }), { action: "toggle" }, `Space plays or pauses on ${view}`);
  eq(actionFor(key(" ", { ...SPACE, shiftKey: true }), { view }), { action: "stop" }, `⇧ Space stops on ${view}`);
  eq(actionFor(key("?", { code: "Slash", shiftKey: true }), { view }), { action: "sheet" }, `? opens the list on ${view}`);
  eq(["1", "2", "3"].map((k) => actionFor(key(k), { view })), [
    { action: "view", id: "synth" }, { action: "view", id: "sequencer" }, { action: "view", id: "match" }], `1 2 3 go to the views from ${view}`);
}
eq(VIEW_KEYS, { 1: "synth", 2: "sequencer", 3: "match" }, "the view keys follow the nav order");
eq(actionFor(key("1", { code: "Numpad1" }), { view: "match" }), { action: "view", id: "synth" }, "the keypad's digits work too");
ok(actionFor(key("!", { code: "Digit1", shiftKey: true }), { view: "synth" }) === null, "⇧ 1 is ! on a US keyboard: nothing");

eq(actionFor(key("-", { code: "Minus" }), { view: "sequencer" }), { action: "tempo", delta: -1 }, "− lowers the tempo by 1");
eq(actionFor(key("=", { code: "Equal" }), { view: "sequencer" }), { action: "tempo", delta: 1 }, "= raises it by 1");
eq(actionFor(key("_", { code: "Minus", shiftKey: true }), { view: "sequencer" }), { action: "tempo", delta: -10 }, "⇧ − (sent as _) lowers it by 10");
eq(actionFor(key("+", { code: "Equal", shiftKey: true }), { view: "sequencer" }), { action: "tempo", delta: 10 }, "⇧ = (sent as +) raises it by 10");
eq(actionFor(key("+", { code: "NumpadAdd" }), { view: "sequencer" }), { action: "tempo", delta: 1 }, "the keypad's + raises it by 1");
eq(actionFor(key("-", { code: "NumpadSubtract" }), { view: "sequencer" }), { action: "tempo", delta: -1 }, "the keypad's − lowers it by 1");
eq(actionFor(key("Delete"), { view: "sequencer" }), { action: "delete" }, "Delete deletes the selected note");
eq(actionFor(key("Backspace"), { view: "sequencer" }), { action: "delete" }, "so does Backspace (the Mac's delete key)");
for (const view of ["synth", "match"]) {
  ok(["-", "=", "_", "+", "Delete", "Backspace"].every((k) => actionFor(key(k), { view }) === null), `tempo and Delete act only on the Sequencer (not ${view})`);
}
ok(["a", "z", "x", "Enter", "Escape", "ArrowUp", "[", "]", "Tab", "4"].every((k) => actionFor(key(k), { view: "sequencer" }) === null),
  "keys that belong to others (the synth's A to K, Z and X; the roll's; Esc on a drawer) are not taken");

for (const mod of ["metaKey", "ctrlKey", "altKey"]) {
  ok([" ", "1", "?", "-", "Delete"].every((k) => actionFor(key(k, { [mod]: true, code: k === " " ? "Space" : "" }), { view: "sequencer" }) === null),
    `nothing acts with ${mod.replace("Key", "")} held (Cmd 1 switches browser tabs)`);
}
ok(actionFor(key(" ", { ...SPACE, isComposing: true }), { view: "synth" }) === null, "nothing acts mid-composition (an input method)");
ok(actionFor(null) === null, "no event, no action");

eq(actionFor(key(" ", SPACE), { view: "synth", sheet: true }), { action: "toggle" }, "with the list open, Space still plays or pauses");
eq(actionFor(key("?", { shiftKey: true }), { view: "synth", sheet: true }), { action: "sheet" }, "and ? closes the list");
ok(["1", "-", "Delete"].every((k) => actionFor(key(k), { view: "sequencer", sheet: true }) === null), "the rest wait until the list closes");
ok(isSpace({ key: " " }) && isSpace({ key: "x", code: "Space" }) && !isSpace({ key: "Enter", code: "Enter" }), "Space by key or by code");

// ── the typing guard (core/keys.js owns it; the synth's keys use the same one) ────────
ok(["INPUT", "TEXTAREA", "SELECT"].every((tagName) => typing({ tagName })), "inputs, text areas and selects take typing");
ok(typing({ tagName: "DIV", isContentEditable: true }), "so does contenteditable");
ok(!typing({ tagName: "BUTTON" }) && !typing({ tagName: "CANVAS" }) && !typing(null) && !typing({ tagName: "BODY" }), "buttons, the roll and the page do not");

// ── the handler, on a fake window ───────────────────────────────────────────────────
function rig({ view = "sequencer" } = {}) {
  const listeners = { keydown: new Set(), keyup: new Set() };
  const target = {
    addEventListener: (t, fn) => listeners[t].add(fn),
    removeEventListener: (t, fn) => listeners[t].delete(fn),
  };
  const log = [];
  const transport = { toggle: () => log.push("toggle"), stop: () => log.push("stop"), nudgeTempo: (d) => log.push(`tempo ${d}`) };
  const sheet = { open: false, toggle() { this.open = !this.open; log.push(this.open ? "sheet open" : "sheet closed"); }, hide() { this.open = false; } };
  const state = { view };
  const keys = createShortcuts({ transport }, { go: (id) => log.push(`go ${id}`), view: () => state.view, target, sheet });
  const fire = (type, k, extra = {}) => {
    const e = { ...key(k, extra), target: extra.target || { tagName: "BUTTON" }, defaultPrevented: Boolean(extra.defaultPrevented), repeat: Boolean(extra.repeat),
      prevented: false, preventDefault() { this.prevented = true; this.defaultPrevented = true; } };
    for (const fn of [...listeners[type]]) fn(e);
    return e;
  };
  return { keys, fire, log, state, listeners, sheet };
}
{
  const r = rig({ view: "synth" });
  const down = r.fire("keydown", " ", SPACE), up = r.fire("keyup", " ", SPACE);
  ok(down.prevented && up.prevented && r.log.join() === "toggle", "Space on a focused button: plays, and neither its keydown nor its keyup clicks the button");
  const held = r.fire("keydown", " ", { ...SPACE, repeat: true });
  ok(held.prevented && r.log.length === 1, "holding Space does not flip it again (and the repeat is still kept from the button)");
  r.fire("keyup", " ", SPACE);
  r.fire("keydown", " ", { ...SPACE, shiftKey: true });
  ok(r.log.at(-1) === "stop", "⇧ Space stops");

  const n = r.log.length;
  const inField = r.fire("keydown", " ", { ...SPACE, target: { tagName: "INPUT" } });
  const inFieldUp = r.fire("keyup", " ", { ...SPACE, target: { tagName: "INPUT" } });
  ok(!inField.prevented && !inFieldUp.prevented && r.log.length === n, "typing a space in a field: nothing acts, nothing is prevented");
  const taken = r.fire("keydown", " ", { ...SPACE, defaultPrevented: true, target: { tagName: "DIV" } });
  const takenUp = r.fire("keyup", " ", SPACE);
  ok(taken.defaultPrevented && !takenUp.prevented && r.log.length === n, "a control that took Space (the Match well) keeps it");

  r.fire("keydown", "2", { code: "Digit2" });
  ok(r.log.at(-1) === "go sequencer", "2 goes to the Sequencer");
  const tempo = r.fire("keydown", "-", { code: "Minus" });
  ok(!tempo.prevented && r.log.at(-1) === "go sequencer", "− on the Synth view does nothing (and keeps its default)");
}
{
  const r = rig({ view: "sequencer" });
  r.fire("keydown", "=", { code: "Equal" });
  r.fire("keydown", "=", { code: "Equal", repeat: true });
  r.fire("keydown", "_", { code: "Minus", shiftKey: true });
  eq(r.log, ["tempo 1", "tempo 1", "tempo -10"], "tempo keys repeat while held; ⇧ goes by 10");

  const none = r.fire("keydown", "Delete");
  ok(!none.prevented, "Delete with no view action bound: left alone");
  let selected = true;
  const off = r.keys.handle("delete", () => { if (!selected) return false; selected = false; r.log.push("deleted"); return true; });
  const del = r.fire("keydown", "Backspace");
  ok(del.prevented && r.log.at(-1) === "deleted", "Delete: the view's action deletes the selected note");
  const again = r.fire("keydown", "Backspace");
  ok(!again.prevented && r.log.filter((x) => x === "deleted").length === 1, "nothing selected: the key is left alone");
  const typed = r.fire("keydown", "Backspace", { target: { tagName: "INPUT" } });
  selected = true;
  ok(!typed.prevented && r.log.filter((x) => x === "deleted").length === 1, "Backspace in the name field edits the name, never the roll");
  off();
  r.fire("keydown", "Delete");
  ok(r.log.filter((x) => x === "deleted").length === 1, "handle() returns off(): an unmounted view's action is gone");

  r.fire("keydown", "?", { code: "Slash", shiftKey: true });
  ok(r.sheet.open && r.log.at(-1) === "sheet open", "? opens the list");
  const blocked = r.fire("keydown", "1", { code: "Digit1" });
  ok(!blocked.prevented && r.log.at(-1) === "sheet open", "with the list open, 1 waits");
  r.fire("keydown", " ", SPACE);
  ok(r.log.at(-1) === "toggle", "Space still plays with the list open");
  r.fire("keyup", " ", SPACE);
  r.fire("keydown", "?", { code: "Slash", shiftKey: true });
  ok(!r.sheet.open, "? again closes it");

  r.keys.destroy();
  ok(r.listeners.keydown.size === 0 && r.listeners.keyup.size === 0, "destroy() removes both listeners");
}

// ── the list of keys ──────────────────────────────────────────────────────
const caps = SHEET.flatMap((g) => g.rows.flatMap((r) => r.keys));
for (const k of ["Space", "⇧ Space", "1", "2", "3", "?", "Esc", "A", "K", "Z", "X", "−", "=", "Delete", "Enter", "[", "]", "←", "→"]) {
  ok(caps.includes(k), `the list names ${k}`);
}
eq(SHEET.map((g) => g.title), ["Everywhere", "Synth", "Sequencer"], "grouped by where the keys act");
const words = SHEET.flatMap((g) => [g.title, ...g.rows.map((r) => r.does)]);
ok(words.every((w) => /^[A-Z]/.test(w) && !/→/.test(w) && !/\b[A-Z]{4,}\b/.test(w)), "sentence case, no arrows in words, no all-caps");

console.log(`shortcuts: ${checks} checks passed`);
